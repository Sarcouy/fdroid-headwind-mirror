from __future__ import annotations

from pathlib import Path

from fdroid_headwind_mirror.state.repository import StateRepository


def track(repository: StateRepository, pkg: str, application_id: int | None = 7) -> None:
    repository.upsert_tracked_package(
        pkg=pkg,
        repo_url="https://f-droid.org/repo",
        auto_approve=False,
        paused=False,
        hmdm_application_id=application_id,
    )


def test_migrations_are_idempotent(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    with StateRepository(database) as repository:
        track(repository, "org.example.app")
    with StateRepository(database) as repository:
        assert [entry.pkg for entry in repository.list_tracked_packages()] == ["org.example.app"]


def test_upsert_updates_existing_row(repository: StateRepository) -> None:
    track(repository, "org.example.app", application_id=7)
    track(repository, "org.example.app", application_id=9)

    packages = repository.list_tracked_packages()
    assert len(packages) == 1
    assert packages[0].hmdm_application_id == 9


def test_new_package_has_no_signer_and_no_version_codes(repository: StateRepository) -> None:
    track(repository, "org.example.app")
    stored = repository.get_tracked_package("org.example.app")

    assert stored is not None
    assert stored.expected_signer is None
    assert stored.last_seen_version_code is None
    assert stored.last_created_version_code is None
    assert stored.last_pushed_version_code is None
    assert stored.awaiting_approval is False


def test_awaiting_approval_when_created_ahead_of_pushed(repository: StateRepository) -> None:
    track(repository, "org.example.app")
    repository.set_version_progress(
        "org.example.app", last_created_version_code=200, last_pushed_version_code=100
    )

    stored = repository.get_tracked_package("org.example.app")
    assert stored is not None
    assert stored.awaiting_approval is True


def test_awaiting_approval_when_never_pushed(repository: StateRepository) -> None:
    track(repository, "org.example.app")
    repository.set_version_progress("org.example.app", last_created_version_code=200)

    stored = repository.get_tracked_package("org.example.app")
    assert stored is not None
    assert stored.awaiting_approval is True


def test_version_progress_is_not_reset_by_upsert(repository: StateRepository) -> None:
    track(repository, "org.example.app")
    repository.set_version_progress(
        "org.example.app", last_created_version_code=200, last_pushed_version_code=200
    )
    track(repository, "org.example.app", application_id=9)

    stored = repository.get_tracked_package("org.example.app")
    assert stored is not None
    assert stored.last_pushed_version_code == 200
    assert stored.awaiting_approval is False


def test_run_lifecycle_and_events(repository: StateRepository) -> None:
    run_id = repository.start_run()
    repository.record_event(run_id, "WARNING", "package.not_in_headwind", "absent", pkg="org.a")
    repository.finish_run(run_id, "WARNING", packages_checked=1, errors=1)

    assert repository.count_events(run_id) == 1


def test_delete_tracked_packages(repository: StateRepository) -> None:
    track(repository, "org.example.app")
    track(repository, "org.example.other")
    repository.delete_tracked_packages(["org.example.other"])

    assert [entry.pkg for entry in repository.list_tracked_packages()] == ["org.example.app"]
