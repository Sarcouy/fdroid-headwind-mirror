from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from fdroid_headwind_mirror.state.repository import StateRepository, SyncEvent, SyncRun

_ERROR = "ERROR"


class PendingApproval(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pkg: str
    created_version_code: int | None
    pushed_version_code: int | None


class RunReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    last_run: SyncRun | None = None
    errors: list[SyncEvent] = Field(default_factory=list)
    history: list[SyncRun] = Field(default_factory=list)
    pending_approvals: list[PendingApproval] = Field(default_factory=list)
    tracked_packages: int = 0

    @property
    def healthy(self) -> bool:
        return self.last_run is not None and self.last_run.errors == 0

    @property
    def unfinished(self) -> bool:
        return self.last_run is not None and self.last_run.finished_at is None


def build_report(repository: StateRepository, *, runs: int = 5) -> RunReport:
    history = repository.list_runs(runs)
    tracked = repository.list_tracked_packages()
    last_run = history[0] if history else None
    return RunReport(
        last_run=last_run,
        errors=repository.list_events(last_run.id, _ERROR) if last_run else [],
        history=history,
        pending_approvals=[
            PendingApproval(
                pkg=entry.pkg,
                created_version_code=entry.last_created_version_code,
                pushed_version_code=entry.last_pushed_version_code,
            )
            for entry in tracked
            if entry.awaiting_approval
        ],
        tracked_packages=len(tracked),
    )
