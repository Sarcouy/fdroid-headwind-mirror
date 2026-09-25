from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from fdroid_headwind_mirror.domain.planner import (
    PackagePlan,
    PlanStatus,
    SyncPlan,
    same_name_version,
)
from fdroid_headwind_mirror.headwind.client import HeadwindClient
from fdroid_headwind_mirror.headwind.errors import HeadwindError
from fdroid_headwind_mirror.headwind.models import ApplicationVersion, NewApplicationVersion
from fdroid_headwind_mirror.state.repository import StateRepository

_URL_FIELD_BY_ARCH: dict[str, str] = {"arm64": "url_arm64", "armeabi": "url_armeabi"}


class PublicationOutcome(StrEnum):
    CREATED = "CREATED"
    REWRITTEN = "REWRITTEN"
    BLOCKED = "BLOCKED"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"


class PublicationEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pkg: str
    outcome: PublicationOutcome
    detail: str = ""
    version: str | None = None
    version_code: int | None = None
    version_id: int | None = None
    configurations: int | None = None
    latest_version_switched: bool | None = None


class PublicationSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entries: list[PublicationEntry] = Field(default_factory=list)

    def _count(self, outcome: PublicationOutcome) -> int:
        return sum(1 for entry in self.entries if entry.outcome is outcome)

    @property
    def created(self) -> int:
        return self._count(PublicationOutcome.CREATED)

    @property
    def rewritten(self) -> int:
        return self._count(PublicationOutcome.REWRITTEN)

    @property
    def blocked(self) -> int:
        return self._count(PublicationOutcome.BLOCKED)

    @property
    def skipped(self) -> int:
        return self._count(PublicationOutcome.SKIPPED)

    @property
    def failed(self) -> int:
        return self._count(PublicationOutcome.FAILED)

    @property
    def configurations(self) -> int:
        return sum(entry.configurations or 0 for entry in self.entries)


def publish_plan(
    plan: SyncPlan,
    client: HeadwindClient,
    repository: StateRepository,
    *,
    run_id: int,
) -> PublicationSummary:
    return PublicationSummary(
        entries=[
            _publish_one(entry, client, repository, run_id=run_id)
            for entry in plan.packages
            if entry.status is PlanStatus.UPDATE_AVAILABLE
        ]
    )


def _publish_one(
    entry: PackagePlan,
    client: HeadwindClient,
    repository: StateRepository,
    *,
    run_id: int,
) -> PublicationEntry:
    prepared = _prepare(entry, repository)
    if isinstance(prepared, str):
        repository.record_event(run_id, "WARNING", "publish.skipped", prepared, pkg=entry.pkg)
        return _entry(entry, PublicationOutcome.SKIPPED, prepared)

    # Read here rather than taken from the plan: the APK verification runs in between, and only
    # the current state tells whether Headwind will create a version or rewrite one in place.
    try:
        versions = client.get_application_versions(prepared.application_id)
    except HeadwindError as exc:
        detail = f"versions illisibles, publication annulee: {exc}"
        repository.record_event(run_id, "ERROR", "publish.aborted", detail, pkg=entry.pkg)
        return _entry(entry, PublicationOutcome.FAILED, detail)

    homonym = same_name_version(versions, prepared.version)
    if homonym is not None:
        return _blocked(entry, homonym, repository, run_id)

    return _create(
        entry, prepared, {version.id for version in versions}, client, repository, run_id=run_id
    )


def _blocked(
    entry: PackagePlan, homonym: ApplicationVersion, repository: StateRepository, run_id: int
) -> PublicationEntry:
    # Blocked whatever auto_approve says. Headwind holds a single version per name: creating it
    # rewrites the same-name version in place, configuration links included, so that its
    # devices receive the new build without any linking and the old one is lost.
    detail = (
        f"version {homonym.version} deja presente dans Headwind (#{homonym.id},"
        f" {_code_label(homonym.version_code)}): Headwind la reecrirait en place avec ses"
        " rattachements aux configurations, publication bloquee"
    )
    repository.record_event(run_id, "WARNING", "publish.rewrite_blocked", detail, pkg=entry.pkg)
    return _entry(entry, PublicationOutcome.BLOCKED, detail, version_id=homonym.id)


def _code_label(version_code: int | None) -> str:
    return "sans versionCode" if version_code is None else f"versionCode {version_code}"


def _create(
    entry: PackagePlan,
    prepared: NewApplicationVersion,
    existing_ids: set[int],
    client: HeadwindClient,
    repository: StateRepository,
    *,
    run_id: int,
) -> PublicationEntry:
    # The links are read before the creation: if a configuration carries autoUpdate, Headwind
    # moves it to the new version on insertion, and the previous state can no longer be seen.
    try:
        links = client.get_application_configurations(prepared.application_id)
    except HeadwindError as exc:
        detail = f"configurations illisibles, publication annulee: {exc}"
        repository.record_event(run_id, "ERROR", "publish.aborted", detail, pkg=entry.pkg)
        return _entry(entry, PublicationOutcome.FAILED, detail)

    configurations = sum(1 for link in links if link.installs_application)

    try:
        created = client.create_application_version(prepared)
    except HeadwindError as exc:
        detail = f"creation refusee par Headwind: {exc}"
        repository.record_event(run_id, "ERROR", "publish.failed", detail, pkg=entry.pkg)
        return _entry(entry, PublicationOutcome.FAILED, detail, configurations=configurations)

    # Recorded for an in-place rewrite as well: the write happened, and leaving it out would
    # have it repeated on every run.
    repository.set_version_progress(entry.pkg, last_created_version_code=prepared.version_code)
    if created is not None and created.id in existing_ids:
        return _rewritten(entry, created.id, configurations, repository, run_id)

    switched = (
        None
        if created is None
        else _latest_version_switched(prepared.application_id, created.id, client)
    )
    detail = _creation_detail(configurations, switched)
    repository.record_event(
        run_id,
        "INFO" if switched is True else "WARNING",
        "publish.created",
        f"Version {prepared.version} creee. {detail}",
        pkg=entry.pkg,
    )
    return _entry(
        entry,
        PublicationOutcome.CREATED,
        detail,
        version_id=created.id if created else None,
        configurations=configurations,
        switched=switched,
    )


def _rewritten(
    entry: PackagePlan,
    version_id: int,
    configurations: int,
    repository: StateRepository,
    run_id: int,
) -> PublicationEntry:
    detail = (
        f"Headwind a reecrit en place la version existante #{version_id} au lieu d'en creer une,"
        f" {configurations} configuration(s) concernee(s): celles rattachees a cette version"
        " deploient ce build sans rattachement explicite"
    )
    repository.record_event(run_id, "WARNING", "publish.rewritten", detail, pkg=entry.pkg)
    return _entry(
        entry,
        PublicationOutcome.REWRITTEN,
        detail,
        version_id=version_id,
        configurations=configurations,
    )


def _prepare(entry: PackagePlan, repository: StateRepository) -> NewApplicationVersion | str:
    incomplete = _incompleteness(entry)
    if incomplete is not None:
        return incomplete

    tracked = repository.get_tracked_package(entry.pkg)
    created = tracked.last_created_version_code if tracked else None
    if created is not None and entry.candidate_version_code is not None:
        if created >= entry.candidate_version_code:
            return _already_created_detail(entry, created)

    urls = _urls(entry)
    if isinstance(urls, str):
        return urls

    return NewApplicationVersion(
        application_id=entry.application_id,
        version=entry.candidate_version,
        version_code=entry.candidate_version_code,
        split=bool(entry.split),
        **urls,
    )


def _already_created_detail(entry: PackagePlan, created: int) -> str:
    # Without this distinction, a package stuck for want of linking would look like a healthy
    # one from the second run on: the plan offers the update again, and the idempotence guard
    # silently discards it.
    if entry.headwind_version_code is not None and entry.headwind_version_code < created:
        return (
            f"version {created} deja creee mais Headwind pointe toujours"
            f" {entry.headwind_version_code}: rattachement explicite requis"
        )
    return f"version {created} deja creee lors d'un run precedent"


def _incompleteness(entry: PackagePlan) -> str | None:
    if entry.candidate_version is None or entry.candidate_version_code is None:
        return "version candidate incomplete"
    if entry.application_id is None:
        return "application Headwind inconnue"
    if not entry.artifacts:
        return "aucun artefact a publier"

    unverified = [item.abi for item in entry.artifacts if item.verified is not True]
    if unverified:
        return f"APK non verifie ({', '.join(unverified)}), publication refusee"
    return None


def _urls(entry: PackagePlan) -> dict[str, str] | str:
    if not entry.split:
        if len(entry.artifacts) != 1:
            return f"version non split avec {len(entry.artifacts)} artefacts"
        return {"url": entry.artifacts[0].url}

    unknown = [
        item.headwind_arch
        for item in entry.artifacts
        if item.headwind_arch not in _URL_FIELD_BY_ARCH
    ]
    if unknown:
        return f"architecture sans champ Headwind ({', '.join(unknown)})"
    return {_URL_FIELD_BY_ARCH[item.headwind_arch]: item.url for item in entry.artifacts}


def _latest_version_switched(
    application_id: int, version_id: int, client: HeadwindClient
) -> bool | None:
    try:
        application = client.get_application(application_id)
    except HeadwindError:
        return None
    return application.latest_version == version_id


def _creation_detail(configurations: int, switched: bool | None) -> str:
    if switched is None:
        return f"{configurations} configuration(s) concernee(s), coherence non verifiable"
    if switched:
        return f"{configurations} configuration(s) concernee(s), latestVersion bascule"
    return (
        f"{configurations} configuration(s) concernee(s), "
        "latestVersion inchange: le rattachement explicite reste necessaire"
    )


def _entry(
    plan: PackagePlan,
    outcome: PublicationOutcome,
    detail: str,
    *,
    version_id: int | None = None,
    configurations: int | None = None,
    switched: bool | None = None,
) -> PublicationEntry:
    return PublicationEntry(
        pkg=plan.pkg,
        outcome=outcome,
        detail=detail,
        version=plan.candidate_version,
        version_code=plan.candidate_version_code,
        version_id=version_id,
        configurations=configurations,
        latest_version_switched=switched,
    )
