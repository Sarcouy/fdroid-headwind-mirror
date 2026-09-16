from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from fdroid_headwind_mirror.config import ResolvedPackage
from fdroid_headwind_mirror.domain.reconciler import PackageStatus, ReconciliationReport
from fdroid_headwind_mirror.fdroid.models import Index
from fdroid_headwind_mirror.fdroid.resolver import Candidate, resolve
from fdroid_headwind_mirror.headwind.client import HeadwindClient
from fdroid_headwind_mirror.headwind.errors import HeadwindError
from fdroid_headwind_mirror.state.repository import StateRepository


class PlanStatus(StrEnum):
    UPDATE_AVAILABLE = "UPDATE_AVAILABLE"
    UP_TO_DATE = "UP_TO_DATE"
    REJECTED = "REJECTED"
    NOT_IN_FDROID = "NOT_IN_FDROID"
    SKIPPED = "SKIPPED"


class SignerState(StrEnum):
    PINNED_NOW = "PINNED_NOW"
    MATCH = "MATCH"
    MISMATCH = "MISMATCH"
    UNKNOWN = "UNKNOWN"


class ArtifactPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    abi: str
    headwind_arch: str
    version_code: int
    url: str
    sha256: str
    size: int | None
    verified: bool | None = None
    reused: bool | None = None
    local_path: str | None = None


class PackagePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pkg: str
    status: PlanStatus
    detail: str = ""
    headwind_version: str | None = None
    headwind_version_code: int | None = None
    candidate_version: str | None = None
    candidate_version_code: int | None = None
    split: bool | None = None
    shape_change: bool = False
    signer_state: SignerState = SignerState.UNKNOWN
    expected_signer: str | None = None
    candidate_signer: str | None = None
    skipped_prereleases: int = 0
    artifacts: list[ArtifactPlan] = Field(default_factory=list)


class SyncPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: int
    index_source: str
    index_timestamp: int
    index_package_count: int
    packages: list[PackagePlan] = Field(default_factory=list)

    @property
    def updates(self) -> int:
        return sum(1 for p in self.packages if p.status is PlanStatus.UPDATE_AVAILABLE)

    @property
    def rejections(self) -> int:
        blocking = {PlanStatus.REJECTED, PlanStatus.NOT_IN_FDROID}
        return sum(1 for p in self.packages if p.status in blocking)

    @property
    def up_to_date(self) -> int:
        return sum(1 for p in self.packages if p.status is PlanStatus.UP_TO_DATE)


def build_plan(
    declared: list[ResolvedPackage],
    reconciliation: ReconciliationReport,
    index: Index,
    client: HeadwindClient,
    repository: StateRepository,
    *,
    run_id: int,
    index_source: str,
    index_timestamp: int,
) -> SyncPlan:
    by_pkg = {entry.pkg: entry for entry in declared}
    plans = [
        _plan_one(
            by_pkg[report.pkg],
            report.status,
            report.application_id,
            index=index,
            client=client,
            repository=repository,
            run_id=run_id,
        )
        for report in reconciliation.packages
        if report.pkg in by_pkg
    ]
    return SyncPlan(
        run_id=run_id,
        index_source=index_source,
        index_timestamp=index_timestamp,
        index_package_count=len(index.packages),
        packages=plans,
    )


def _plan_one(
    entry: ResolvedPackage,
    headwind_status: PackageStatus,
    application_id: int | None,
    *,
    index: Index,
    client: HeadwindClient,
    repository: StateRepository,
    run_id: int,
) -> PackagePlan:
    if headwind_status is PackageStatus.PAUSED:
        return PackagePlan(pkg=entry.pkg, status=PlanStatus.SKIPPED, detail="suivi suspendu")
    if application_id is None:
        return PackagePlan(
            pkg=entry.pkg,
            status=PlanStatus.SKIPPED,
            detail="non resolu dans Headwind, voir la commande status",
        )

    package = index.packages.get(entry.pkg)
    if package is None:
        repository.record_event(
            run_id, "WARNING", "fdroid.not_found", "Paquet absent du depot F-Droid", pkg=entry.pkg
        )
        return PackagePlan(
            pkg=entry.pkg, status=PlanStatus.NOT_IN_FDROID, detail="absent du depot F-Droid"
        )

    resolution = resolve(entry.pkg, package, entry.target_abis, entry.blocked_anti_features)
    if resolution.candidate is None:
        detail = resolution.rejection.detail if resolution.rejection else "version non resolue"
        repository.record_event(run_id, "ERROR", "fdroid.unresolved", detail, pkg=entry.pkg)
        return PackagePlan(
            pkg=entry.pkg,
            status=PlanStatus.REJECTED,
            detail=detail,
            skipped_prereleases=resolution.skipped_prereleases,
        )

    candidate = resolution.candidate
    signer_state, expected = _check_signer(entry.pkg, candidate, repository, run_id)
    headwind = _headwind_state(application_id, client)

    if not headwind.readable:
        repository.record_event(
            run_id,
            "ERROR",
            "headwind.version_unreadable",
            "Versions Headwind illisibles, comparaison impossible",
            pkg=entry.pkg,
        )
        return PackagePlan(
            pkg=entry.pkg,
            status=PlanStatus.SKIPPED,
            detail="versions Headwind illisibles, comparaison impossible",
            candidate_version=candidate.version_name,
            candidate_version_code=candidate.version_code,
            signer_state=signer_state,
            expected_signer=expected,
            candidate_signer=candidate.signer,
        )

    status = _status(candidate, headwind.version_code, signer_state)
    # Repere informatif: la derniere version observee sur F-Droid, meme refusee, pour que le
    # rapport reste lisible sans relire l'index. Ne sert jamais a decider d'une publication,
    # ce role revenant a last_created_version_code et last_pushed_version_code.
    repository.set_version_progress(entry.pkg, last_seen_version_code=candidate.version_code)

    return PackagePlan(
        pkg=entry.pkg,
        status=status,
        detail=_detail(status, signer_state),
        headwind_version=headwind.version,
        headwind_version_code=headwind.version_code,
        candidate_version=candidate.version_name,
        candidate_version_code=candidate.version_code,
        split=candidate.split,
        shape_change=headwind.split is not None and headwind.split != candidate.split,
        signer_state=signer_state,
        expected_signer=expected,
        candidate_signer=candidate.signer,
        skipped_prereleases=resolution.skipped_prereleases,
        artifacts=[
            ArtifactPlan(
                abi=artifact.abi,
                headwind_arch=artifact.headwind_arch,
                version_code=artifact.version_code,
                url=artifact.url(entry.repo_url),
                sha256=artifact.sha256,
                size=artifact.size,
            )
            for artifact in candidate.artifacts
        ],
    )


def _check_signer(
    pkg: str, candidate: Candidate, repository: StateRepository, run_id: int
) -> tuple[SignerState, str | None]:
    stored = repository.get_tracked_package(pkg)
    expected = stored.expected_signer if stored else None

    if expected is None:
        repository.pin_expected_signer(pkg, candidate.signer)
        repository.record_event(
            run_id, "INFO", "signer.pinned", f"Signataire epingle: {candidate.signer}", pkg=pkg
        )
        return SignerState.PINNED_NOW, candidate.signer

    if expected == candidate.signer:
        return SignerState.MATCH, expected

    repository.record_event(
        run_id,
        "ERROR",
        "signer.mismatch",
        f"Signataire divergent: epingle {expected}, candidat {candidate.signer}",
        pkg=pkg,
    )
    return SignerState.MISMATCH, expected


class HeadwindState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str | None = None
    version_code: int | None = None
    split: bool | None = None
    readable: bool = True


def _headwind_state(application_id: int, client: HeadwindClient) -> HeadwindState:
    try:
        versions = client.get_application_versions(application_id)
    except HeadwindError:
        return HeadwindState(readable=False)
    if not versions:
        return HeadwindState()
    latest = max(versions, key=lambda version: version.version_code or 0)
    return HeadwindState(
        version=latest.version, version_code=latest.version_code, split=latest.split
    )


def _status(
    candidate: Candidate, headwind_code: int | None, signer_state: SignerState
) -> PlanStatus:
    if signer_state is SignerState.MISMATCH:
        return PlanStatus.REJECTED
    if headwind_code is not None and candidate.version_code <= headwind_code:
        return PlanStatus.UP_TO_DATE
    return PlanStatus.UPDATE_AVAILABLE


def _detail(status: PlanStatus, signer_state: SignerState) -> str:
    if status is PlanStatus.REJECTED and signer_state is SignerState.MISMATCH:
        return "signataire divergent"
    if signer_state is SignerState.PINNED_NOW:
        return "signataire epingle a cette execution"
    return ""
