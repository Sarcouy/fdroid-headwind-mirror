from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from fdroid_headwind_mirror.domain.planner import PackagePlan, SyncPlan
from fdroid_headwind_mirror.headwind.client import HeadwindClient
from fdroid_headwind_mirror.headwind.errors import HeadwindError
from fdroid_headwind_mirror.state.repository import StateRepository, TrackedPackage

_INSTALL_ACTION = 1
_UNINSTALL_ACTION = 2


class LinkingOutcome(StrEnum):
    LINKED = "LINKED"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"


class LinkingEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pkg: str
    outcome: LinkingOutcome
    detail: str = ""
    version_code: int | None = None
    version_id: int | None = None
    configurations: int | None = None
    notify_requested: bool = False


class LinkingSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entries: list[LinkingEntry] = Field(default_factory=list)

    def _count(self, outcome: LinkingOutcome) -> int:
        return sum(1 for entry in self.entries if entry.outcome is outcome)

    @property
    def linked(self) -> int:
        return self._count(LinkingOutcome.LINKED)

    @property
    def skipped(self) -> int:
        return self._count(LinkingOutcome.SKIPPED)

    @property
    def failed(self) -> int:
        return self._count(LinkingOutcome.FAILED)

    @property
    def configurations(self) -> int:
        return sum(entry.configurations or 0 for entry in self.entries)

    @property
    def awaiting_approval(self) -> list[str]:
        return [
            entry.pkg
            for entry in self.entries
            if entry.outcome is LinkingOutcome.SKIPPED and entry.version_id is None
        ]


def link_plan(
    plan: SyncPlan,
    client: HeadwindClient,
    repository: StateRepository,
    *,
    run_id: int,
) -> LinkingSummary:
    entries: list[LinkingEntry] = []
    for entry in plan.packages:
        tracked = repository.get_tracked_package(entry.pkg)
        # A version created by this run already has last_created > last_pushed: the same condition
        # covers both what was just published and what a previous run left unlinked.
        if tracked is None or not tracked.awaiting_approval:
            continue
        entries.append(_link_one(entry, tracked, client, repository, run_id=run_id))
    return LinkingSummary(entries=entries)


def _link_one(
    entry: PackagePlan,
    tracked: TrackedPackage,
    client: HeadwindClient,
    repository: StateRepository,
    *,
    run_id: int,
) -> LinkingEntry:
    version_code = tracked.last_created_version_code
    if not tracked.auto_approve:
        detail = "manual approval required, linking not performed"
        repository.record_event(run_id, "WARNING", "link.awaiting_approval", detail, pkg=entry.pkg)
        return LinkingEntry(
            pkg=entry.pkg,
            outcome=LinkingOutcome.SKIPPED,
            detail=detail,
            version_code=version_code,
        )

    application_id = entry.application_id or tracked.hmdm_application_id
    if application_id is None or version_code is None:
        return _failed(entry, version_code, "unknown Headwind application", repository, run_id)

    target = _resolve_target(application_id, version_code, client)
    if isinstance(target, str):
        return _failed(entry, version_code, target, repository, run_id)
    version_id, configurations = target

    targets = sum(1 for item in configurations if item["action"] == _INSTALL_ACTION)
    if targets == 0:
        return _nothing_to_link(entry, version_code, version_id, repository, run_id)

    try:
        client.link_version_configurations(version_id, configurations)
    except HeadwindError as exc:
        return _failed(entry, version_code, f"linking refused: {exc}", repository, run_id)

    repository.set_version_progress(entry.pkg, last_pushed_version_code=version_code)
    detail = f"{targets} configuration(s) linked, notification requested"
    repository.record_event(run_id, "INFO", "link.done", detail, pkg=entry.pkg)
    return LinkingEntry(
        pkg=entry.pkg,
        outcome=LinkingOutcome.LINKED,
        detail=detail,
        version_code=version_code,
        version_id=version_id,
        configurations=targets,
        notify_requested=True,
    )


def _resolve_target(
    application_id: int, version_code: int, client: HeadwindClient
) -> tuple[int, list[dict[str, Any]]] | str:
    try:
        version_id = _resolve_version_id(application_id, version_code, client)
        if version_id is None:
            return f"version {version_code} not found in Headwind"
        installing = {
            link.configuration_id
            for link in client.get_application_configurations(application_id)
            if link.installs_application
        }
        return version_id, _links_to_send(client.get_version_configurations(version_id), installing)
    except HeadwindError as exc:
        return f"unreadable links: {exc}"


def _links_to_send(candidates: list[dict[str, Any]], installing: set[int]) -> list[dict[str, Any]]:
    # Headwind does not carry the action over from one version to the next: a new version comes
    # back with 0, "do not install", in every configuration. Action 1 is therefore set where a
    # version of the application is installed, unless an uninstall was requested on this one. As
    # in the panel, no row with 0 is sent: the server would insert it as is, creating a link in
    # a configuration that did not install the application.
    links: list[dict[str, Any]] = []
    for item in candidates:
        if item.get("action") == _UNINSTALL_ACTION:
            links.append({**item, "notify": False})
        elif item.get("configurationId") in installing:
            links.append({**item, "action": _INSTALL_ACTION, "notify": True})
    return links


def _resolve_version_id(
    application_id: int, version_code: int, client: HeadwindClient
) -> int | None:
    # The id is not taken from the publication: it may be missing (unusable creation response),
    # and it does not exist at all when linking resumes the work left by a previous run.
    for version in client.get_application_versions(application_id):
        if version.version_code == version_code:
            return version.id
    return None


def _nothing_to_link(
    entry: PackagePlan,
    version_code: int,
    version_id: int,
    repository: StateRepository,
    run_id: int,
) -> LinkingEntry:
    # The progress is recorded even though nothing was done: otherwise the package would stay
    # reported as pending on every run, for a state already reached.
    repository.set_version_progress(entry.pkg, last_pushed_version_code=version_code)
    detail = "no configuration installs this application"
    repository.record_event(run_id, "INFO", "link.nothing", detail, pkg=entry.pkg)
    return LinkingEntry(
        pkg=entry.pkg,
        outcome=LinkingOutcome.SKIPPED,
        detail=detail,
        version_code=version_code,
        version_id=version_id,
        configurations=0,
    )


def _failed(
    entry: PackagePlan,
    version_code: int | None,
    detail: str,
    repository: StateRepository,
    run_id: int,
) -> LinkingEntry:
    repository.record_event(run_id, "ERROR", "link.failed", detail, pkg=entry.pkg)
    return LinkingEntry(
        pkg=entry.pkg,
        outcome=LinkingOutcome.FAILED,
        detail=detail,
        version_code=version_code,
    )
