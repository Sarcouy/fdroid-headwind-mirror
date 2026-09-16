from __future__ import annotations

from collections import defaultdict
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from fdroid_headwind_mirror.config import ResolvedPackage
from fdroid_headwind_mirror.headwind.models import Application
from fdroid_headwind_mirror.state.repository import StateRepository


class PackageStatus(StrEnum):
    RESOLVED = "RESOLVED"
    NOT_IN_HEADWIND = "NOT_IN_HEADWIND"
    AMBIGUOUS = "AMBIGUOUS"
    PAUSED = "PAUSED"


class ApplicationRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: int
    name: str
    version: str | None
    common: bool
    customer_id: int | None

    @classmethod
    def of(cls, application: Application) -> ApplicationRef:
        return cls(
            id=application.id,
            name=application.name,
            version=application.version,
            common=application.common,
            customer_id=application.customer_id,
        )


class PackageReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pkg: str
    status: PackageStatus
    application_id: int | None = None
    candidates: list[ApplicationRef] = Field(default_factory=list)
    headwind_version: str | None = None
    auto_approve: bool
    expected_signer: str | None = None
    last_seen_version_code: int | None = None
    last_pushed_version_code: int | None = None
    awaiting_approval: bool = False


class ReconciliationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: int
    packages: list[PackageReport] = Field(default_factory=list)
    dropped: list[str] = Field(default_factory=list)
    untracked_applications: list[ApplicationRef] = Field(default_factory=list)

    @property
    def blocking_count(self) -> int:
        blocking = {PackageStatus.NOT_IN_HEADWIND, PackageStatus.AMBIGUOUS}
        return sum(1 for report in self.packages if report.status in blocking)


def index_by_package(applications: list[Application]) -> dict[str, list[Application]]:
    index: dict[str, list[Application]] = defaultdict(list)
    for application in applications:
        index[application.pkg].append(application)
    return dict(index)


def reconcile(
    declared: list[ResolvedPackage],
    applications: list[Application],
    repository: StateRepository,
    run_id: int,
) -> ReconciliationReport:
    index = index_by_package(applications)
    declared_names = {entry.pkg for entry in declared}

    dropped = [
        stored.pkg
        for stored in repository.list_tracked_packages()
        if stored.pkg not in declared_names
    ]
    repository.delete_tracked_packages(dropped)
    for pkg in dropped:
        repository.record_event(
            run_id, "INFO", "package.dropped", "Paquet retire de packages.yaml", pkg=pkg
        )

    reports = [
        _reconcile_one(entry, index.get(entry.pkg, []), repository, run_id) for entry in declared
    ]

    untracked = [
        ApplicationRef.of(application)
        for application in applications
        if application.pkg not in declared_names
    ]

    return ReconciliationReport(
        run_id=run_id,
        packages=reports,
        dropped=sorted(dropped),
        untracked_applications=sorted(untracked, key=lambda ref: (ref.name.lower(), ref.id)),
    )


def _reconcile_one(
    entry: ResolvedPackage,
    candidates: list[Application],
    repository: StateRepository,
    run_id: int,
) -> PackageReport:
    status, application = _resolve(entry, candidates)
    application_id = application.id if application is not None else None

    repository.upsert_tracked_package(
        pkg=entry.pkg,
        repo_url=entry.repo_url,
        auto_approve=entry.auto_approve,
        paused=entry.paused,
        hmdm_application_id=application_id,
    )

    _record_resolution_event(entry, status, candidates, repository, run_id)
    stored = repository.get_tracked_package(entry.pkg)

    return PackageReport(
        pkg=entry.pkg,
        status=status,
        application_id=application_id,
        candidates=[ApplicationRef.of(candidate) for candidate in candidates],
        headwind_version=application.version if application is not None else None,
        auto_approve=entry.auto_approve,
        expected_signer=stored.expected_signer if stored else None,
        last_seen_version_code=stored.last_seen_version_code if stored else None,
        last_pushed_version_code=stored.last_pushed_version_code if stored else None,
        awaiting_approval=stored.awaiting_approval if stored else False,
    )


def _resolve(
    entry: ResolvedPackage, candidates: list[Application]
) -> tuple[PackageStatus, Application | None]:
    resolved = candidates[0] if len(candidates) == 1 else None
    if entry.paused:
        return PackageStatus.PAUSED, resolved
    if resolved is not None:
        return PackageStatus.RESOLVED, resolved
    if not candidates:
        return PackageStatus.NOT_IN_HEADWIND, None
    return PackageStatus.AMBIGUOUS, None


def _record_resolution_event(
    entry: ResolvedPackage,
    status: PackageStatus,
    candidates: list[Application],
    repository: StateRepository,
    run_id: int,
) -> None:
    if status is PackageStatus.NOT_IN_HEADWIND:
        repository.record_event(
            run_id,
            "WARNING",
            "package.not_in_headwind",
            "Aucune application Headwind ne porte ce package",
            pkg=entry.pkg,
        )
    elif status is PackageStatus.AMBIGUOUS:
        ids = ", ".join(str(candidate.id) for candidate in candidates)
        repository.record_event(
            run_id,
            "ERROR",
            "package.ambiguous",
            f"Plusieurs applications Headwind portent ce package: {ids}",
            pkg=entry.pkg,
        )
