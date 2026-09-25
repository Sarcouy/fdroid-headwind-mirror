from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from fdroid_headwind_mirror import cli
from fdroid_headwind_mirror.reporting.report import build_report
from fdroid_headwind_mirror.state.repository import StateRepository
from tests.conftest import track_package

runner = CliRunner(mix_stderr=False)


@pytest.fixture(name="workspace")
def fixture_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "packages.yaml").write_text(
        "repo:\n  url: https://f-droid.org/repo\npackages: []\n", encoding="utf-8"
    )
    monkeypatch.setenv("FHM_HEADWIND_URL", "https://mdm.example.org")
    monkeypatch.setenv("FHM_HEADWIND_LOGIN", "service")
    monkeypatch.setenv("FHM_HEADWIND_PASSWORD", "secret")
    monkeypatch.setenv("FHM_PACKAGES_FILE", str(tmp_path / "packages.yaml"))
    monkeypatch.setenv("FHM_DATABASE_PATH", str(tmp_path / "state.db"))
    monkeypatch.setenv("FHM_CACHE_DIR", str(tmp_path / "cache"))
    return tmp_path


def seed(path: Path) -> None:
    with StateRepository(path / "state.db") as repository:
        track_package(repository)
        repository.set_version_progress("org.videolan.vlc", last_created_version_code=13070106)
        run_id = repository.start_run()
        repository.record_event(run_id, "ERROR", "publish.failed", "creation refusee", pkg="a.b")
        repository.record_event(run_id, "INFO", "fdroid.index", "index a jour")
        repository.finish_run(run_id, "WARNING", packages_checked=3, versions_created=1, errors=1)


@pytest.mark.usefixtures("workspace")
def test_an_empty_database_reports_nothing_without_failing() -> None:
    result = runner.invoke(cli.app, ["report"])

    assert result.exit_code == 0
    assert "Aucune execution" in result.stdout


def test_the_report_shows_the_last_run_and_its_errors(workspace: Path) -> None:
    seed(workspace)

    result = runner.invoke(cli.app, ["report"])

    assert result.exit_code == 1
    assert "WARNING" in result.stdout
    assert "creation refusee" in result.stdout
    assert "org.videolan.vlc" in result.stdout


def test_the_json_report_is_machine_readable(workspace: Path) -> None:
    seed(workspace)

    result = runner.invoke(cli.app, ["report", "--json"])

    payload = json.loads(result.stdout)
    assert payload["last_run"]["errors"] == 1
    assert payload["last_run"]["packages_checked"] == 3
    assert [event["code"] for event in payload["errors"]] == ["publish.failed"]
    assert payload["pending_approvals"][0]["pkg"] == "org.videolan.vlc"
    assert payload["tracked_packages"] == 1


def test_only_error_events_are_reported(workspace: Path) -> None:
    seed(workspace)

    with StateRepository(workspace / "state.db") as repository:
        report = build_report(repository)

    assert [event.level for event in report.errors] == ["ERROR"]
    assert report.healthy is False


def test_a_clean_run_is_healthy(workspace: Path) -> None:
    with StateRepository(workspace / "state.db") as repository:
        run_id = repository.start_run()
        repository.finish_run(run_id, "OK", packages_checked=2)
        report = build_report(repository)

    assert report.healthy is True
    assert report.unfinished is False
    assert not report.pending_approvals


def test_an_interrupted_run_is_visible(workspace: Path) -> None:
    with StateRepository(workspace / "state.db") as repository:
        repository.start_run()
        report = build_report(repository)

    assert report.unfinished is True


def test_history_is_limited_and_ordered_from_the_latest(workspace: Path) -> None:
    with StateRepository(workspace / "state.db") as repository:
        for _ in range(4):
            repository.finish_run(repository.start_run(), "OK")
        report = build_report(repository, runs=2)

    assert len(report.history) == 2
    assert report.history[0].id > report.history[1].id


def test_pruning_removes_old_runs_and_their_events(workspace: Path) -> None:
    with StateRepository(workspace / "state.db") as repository:
        old_run = repository.start_run()
        repository.record_event(old_run, "INFO", "a.b", "ancien")
        # pylint: disable=protected-access  # start_run timestamps with now: dating a run in
        # the past requires writing to the database directly, which the API does not expose.
        repository._connection.execute(
            "UPDATE sync_run SET started_at = ? WHERE id = ?",
            ((datetime.now(UTC) - timedelta(days=200)).isoformat(timespec="seconds"), old_run),
        )
        repository._connection.commit()
        recent_run = repository.start_run()
        repository.record_event(recent_run, "INFO", "a.b", "recent")

    result = runner.invoke(cli.app, ["prune", "--days", "90", "--yes"])

    assert result.exit_code == 0
    assert "1 execution(s) et 1 evenement(s)" in result.stdout
    with StateRepository(workspace / "state.db") as repository:
        assert [run.id for run in repository.list_runs(10)] == [recent_run]
        assert repository.count_events(old_run) == 0
        assert repository.count_events(recent_run) == 1


def test_pruning_asks_before_deleting(workspace: Path) -> None:
    with StateRepository(workspace / "state.db") as repository:
        repository.finish_run(repository.start_run(), "OK")

    result = runner.invoke(cli.app, ["prune", "--days", "1"], input="n\n")

    assert result.exit_code == 1
    with StateRepository(workspace / "state.db") as repository:
        assert len(repository.list_runs(10)) == 1
