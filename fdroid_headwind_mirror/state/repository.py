from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Self

from pydantic import BaseModel, ConfigDict

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


class TrackedPackage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pkg: str
    hmdm_application_id: int | None
    repo_url: str
    expected_signer: str | None
    auto_approve: bool
    paused: bool
    last_seen_version_code: int | None
    last_created_version_code: int | None
    last_pushed_version_code: int | None

    @property
    def awaiting_approval(self) -> bool:
        if self.last_created_version_code is None:
            return False
        pushed = self.last_pushed_version_code
        return pushed is None or self.last_created_version_code > pushed


class SyncRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    started_at: str
    finished_at: str | None
    status: str
    packages_checked: int
    versions_created: int
    errors: int


class SyncEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    run_id: int
    pkg: str | None
    level: str
    code: str
    message: str
    created_at: str


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class StateRepository:
    def __init__(self, database_path: Path) -> None:
        self._path = database_path
        if database_path.parent != Path(""):
            database_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(database_path)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._migrate()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._connection.close()

    def _migrate(self) -> None:
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migration ("
            "  name TEXT PRIMARY KEY,"
            "  applied_at TEXT NOT NULL"
            ")"
        )
        applied = {
            str(row["name"])
            for row in self._connection.execute("SELECT name FROM schema_migration")
        }
        for migration in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if migration.name in applied:
                continue
            with self._connection:
                self._connection.executescript(migration.read_text(encoding="utf-8"))
                self._connection.execute(
                    "INSERT INTO schema_migration (name, applied_at) VALUES (?, ?)",
                    (migration.name, _now()),
                )

    def list_tracked_packages(self) -> list[TrackedPackage]:
        rows = self._connection.execute("SELECT * FROM tracked_package ORDER BY pkg").fetchall()
        return [self._to_tracked_package(row) for row in rows]

    def get_tracked_package(self, pkg: str) -> TrackedPackage | None:
        row = self._connection.execute(
            "SELECT * FROM tracked_package WHERE pkg = ?", (pkg,)
        ).fetchone()
        return self._to_tracked_package(row) if row is not None else None

    def upsert_tracked_package(
        self,
        pkg: str,
        *,
        repo_url: str,
        auto_approve: bool,
        paused: bool,
        hmdm_application_id: int | None,
    ) -> None:
        now = _now()
        with self._connection:
            self._connection.execute(
                "INSERT INTO tracked_package ("
                "  pkg, hmdm_application_id, repo_url, auto_approve, paused,"
                "  created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (pkg) DO UPDATE SET "
                "  hmdm_application_id = excluded.hmdm_application_id,"
                "  repo_url = excluded.repo_url,"
                "  auto_approve = excluded.auto_approve,"
                "  paused = excluded.paused,"
                "  updated_at = excluded.updated_at",
                (pkg, hmdm_application_id, repo_url, auto_approve, paused, now, now),
            )

    def pin_expected_signer(self, pkg: str, signer: str) -> None:
        with self._connection:
            self._connection.execute(
                "UPDATE tracked_package SET expected_signer = ?, updated_at = ?"
                " WHERE pkg = ? AND expected_signer IS NULL",
                (signer, _now(), pkg),
            )

    def clear_expected_signer(self, pkg: str) -> None:
        with self._connection:
            self._connection.execute(
                "UPDATE tracked_package SET expected_signer = NULL, updated_at = ? WHERE pkg = ?",
                (_now(), pkg),
            )

    def set_version_progress(
        self,
        pkg: str,
        *,
        last_seen_version_code: int | None = None,
        last_created_version_code: int | None = None,
        last_pushed_version_code: int | None = None,
    ) -> None:
        assignments: list[str] = []
        values: list[object] = []
        for column, value in (
            ("last_seen_version_code", last_seen_version_code),
            ("last_created_version_code", last_created_version_code),
            ("last_pushed_version_code", last_pushed_version_code),
        ):
            if value is not None:
                assignments.append(f"{column} = ?")
                values.append(value)
        if not assignments:
            return
        assignments.append("updated_at = ?")
        values.extend([_now(), pkg])
        with self._connection:
            self._connection.execute(
                f"UPDATE tracked_package SET {', '.join(assignments)} WHERE pkg = ?",
                tuple(values),
            )

    def delete_tracked_packages(self, packages: list[str]) -> None:
        if not packages:
            return
        with self._connection:
            self._connection.executemany(
                "DELETE FROM tracked_package WHERE pkg = ?", [(pkg,) for pkg in packages]
            )

    def start_run(self) -> int:
        with self._connection:
            cursor = self._connection.execute(
                "INSERT INTO sync_run (started_at, status) VALUES (?, ?)",
                (_now(), "RUNNING"),
            )
        return int(cursor.lastrowid or 0)

    def finish_run(
        self,
        run_id: int,
        status: str,
        *,
        packages_checked: int = 0,
        versions_created: int = 0,
        errors: int = 0,
    ) -> None:
        with self._connection:
            self._connection.execute(
                "UPDATE sync_run SET finished_at = ?, status = ?, packages_checked = ?,"
                " versions_created = ?, errors = ? WHERE id = ?",
                (_now(), status, packages_checked, versions_created, errors, run_id),
            )

    def record_event(
        self,
        run_id: int,
        level: str,
        code: str,
        message: str,
        *,
        pkg: str | None = None,
        payload_json: str | None = None,
    ) -> None:
        with self._connection:
            self._connection.execute(
                "INSERT INTO sync_event (run_id, pkg, level, code, message, payload_json,"
                " created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (run_id, pkg, level, code, message, payload_json, _now()),
            )

    def list_runs(self, limit: int = 5) -> list[SyncRun]:
        rows = self._connection.execute(
            "SELECT id, started_at, finished_at, status, packages_checked, versions_created,"
            " errors FROM sync_run ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [SyncRun(**dict(row)) for row in rows]

    def list_events(self, run_id: int, level: str | None = None) -> list[SyncEvent]:
        query = (
            "SELECT id, run_id, pkg, level, code, message, created_at"
            " FROM sync_event WHERE run_id = ?"
        )
        values: list[object] = [run_id]
        if level is not None:
            query += " AND level = ?"
            values.append(level)
        rows = self._connection.execute(f"{query} ORDER BY id", tuple(values)).fetchall()
        return [SyncEvent(**dict(row)) for row in rows]

    def prune_runs(self, before: str) -> tuple[int, int]:
        events = self._connection.execute(
            "SELECT COUNT(*) AS total FROM sync_event WHERE run_id IN"
            " (SELECT id FROM sync_run WHERE started_at < ?)",
            (before,),
        ).fetchone()["total"]
        with self._connection:
            cursor = self._connection.execute(
                "DELETE FROM sync_run WHERE started_at < ?", (before,)
            )
        return cursor.rowcount, int(events)

    def count_events(self, run_id: int) -> int:
        row = self._connection.execute(
            "SELECT COUNT(*) AS total FROM sync_event WHERE run_id = ?", (run_id,)
        ).fetchone()
        return int(row["total"])

    @staticmethod
    def _to_tracked_package(row: sqlite3.Row) -> TrackedPackage:
        return TrackedPackage(
            pkg=str(row["pkg"]),
            hmdm_application_id=row["hmdm_application_id"],
            repo_url=str(row["repo_url"]),
            expected_signer=row["expected_signer"],
            auto_approve=bool(row["auto_approve"]),
            paused=bool(row["paused"]),
            last_seen_version_code=row["last_seen_version_code"],
            last_created_version_code=row["last_created_version_code"],
            last_pushed_version_code=row["last_pushed_version_code"],
        )
