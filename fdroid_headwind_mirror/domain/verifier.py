from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from fdroid_headwind_mirror.domain.planner import PackagePlan, PlanStatus, SyncPlan
from fdroid_headwind_mirror.fdroid.client import FDroidClient, FDroidError
from fdroid_headwind_mirror.fdroid.download import ApkRequest, ApkStore, fetch_apk
from fdroid_headwind_mirror.state.repository import StateRepository


class VerificationSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verified: int = 0
    failed: int = 0
    downloaded_bytes: int = 0
    reused_bytes: int = 0


def verify_plan(
    plan: SyncPlan,
    client: FDroidClient,
    store: ApkStore,
    repository: StateRepository,
    *,
    run_id: int,
) -> VerificationSummary:
    summary = VerificationSummary()
    for entry in plan.packages:
        if entry.status is not PlanStatus.UPDATE_AVAILABLE:
            continue
        _verify_package(entry, client, store, repository, run_id=run_id, summary=summary)
    return summary


def _verify_package(
    entry: PackagePlan,
    client: FDroidClient,
    store: ApkStore,
    repository: StateRepository,
    *,
    run_id: int,
    summary: VerificationSummary,
) -> None:
    for artifact in entry.artifacts:
        try:
            download = fetch_apk(
                client,
                store,
                ApkRequest(
                    pkg=entry.pkg,
                    version_code=artifact.version_code,
                    abi=artifact.abi,
                    url=artifact.url,
                    expected_sha256=artifact.sha256,
                    expected_size=artifact.size,
                ),
            )
        except FDroidError as exc:
            artifact.verified = False
            entry.status = PlanStatus.REJECTED
            entry.detail = f"APK cannot be verified ({artifact.abi}): {exc}"
            summary.failed += 1
            repository.record_event(run_id, "ERROR", "apk.integrity", entry.detail, pkg=entry.pkg)
            return

        artifact.verified = True
        artifact.reused = download.reused
        artifact.local_path = str(download.path)
        summary.verified += 1
        if download.reused:
            summary.reused_bytes += download.size
        else:
            summary.downloaded_bytes += download.size

    repository.record_event(
        run_id,
        "INFO",
        "apk.verified",
        f"{len(entry.artifacts)} APK(s) verified for version {entry.candidate_version_code}",
        pkg=entry.pkg,
    )
