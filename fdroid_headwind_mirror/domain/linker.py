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
        # Une version creee ce run porte deja last_created > last_pushed: la meme condition couvre
        # donc ce qui vient d'etre publie et ce qu'un run precedent a laisse sans rattachement.
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
        detail = "approbation manuelle requise, rattachement non effectue"
        repository.record_event(run_id, "WARNING", "link.awaiting_approval", detail, pkg=entry.pkg)
        return LinkingEntry(
            pkg=entry.pkg,
            outcome=LinkingOutcome.SKIPPED,
            detail=detail,
            version_code=version_code,
        )

    application_id = entry.application_id or tracked.hmdm_application_id
    if application_id is None or version_code is None:
        return _failed(entry, version_code, "application Headwind inconnue", repository, run_id)

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
        return _failed(entry, version_code, f"rattachement refuse: {exc}", repository, run_id)

    repository.set_version_progress(entry.pkg, last_pushed_version_code=version_code)
    detail = f"{targets} configuration(s) rattachee(s), notification demandee"
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
            return f"version {version_code} introuvable dans Headwind"
        installing = {
            link.configuration_id
            for link in client.get_application_configurations(application_id)
            if link.installs_application
        }
        return version_id, _links_to_send(client.get_version_configurations(version_id), installing)
    except HeadwindError as exc:
        return f"liens illisibles: {exc}"


def _links_to_send(candidates: list[dict[str, Any]], installing: set[int]) -> list[dict[str, Any]]:
    # Headwind ne reporte pas l'action d'une version a l'autre: une version neuve revient a 0,
    # "ne pas installer", dans toutes les configurations. L'action 1 est donc posee la ou une
    # version de l'application est installee, sauf desinstallation demandee sur celle-ci. Comme
    # le panneau, aucune ligne a 0 n'est emise: le serveur l'insererait telle quelle, creant un
    # lien dans une configuration qui n'installait pas l'application.
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
    # L'identifiant n'est pas repris de la publication: il peut manquer (reponse de creation
    # inexploitable) et il n'existe pas du tout quand le rattachement reprend le travail laisse
    # par un run precedent.
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
    # La progression est enregistree malgre l'absence d'action: sans cela le paquet resterait
    # signale en attente a chaque execution, pour un etat pourtant deja atteint.
    repository.set_version_progress(entry.pkg, last_pushed_version_code=version_code)
    detail = "aucune configuration n'installe cette application"
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
