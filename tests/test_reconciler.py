from __future__ import annotations

from fdroid_headwind_mirror.config import ResolvedPackage
from fdroid_headwind_mirror.domain.reconciler import PackageStatus, reconcile
from fdroid_headwind_mirror.headwind.models import Application
from fdroid_headwind_mirror.state.repository import StateRepository
from tests.conftest import application_payload


def declared(pkg: str, paused: bool = False) -> ResolvedPackage:
    return ResolvedPackage(
        pkg=pkg,
        repo_url="https://f-droid.org/repo",
        mirror=True,
        auto_approve=False,
        paused=paused,
    )


def application(application_id: int, pkg: str, common: bool = False) -> Application:
    return Application.model_validate(application_payload(application_id, pkg, common=common))


def test_single_candidate_is_resolved_and_persisted(repository: StateRepository) -> None:
    run_id = repository.start_run()
    report = reconcile(
        [declared("org.example.app")], [application(7, "org.example.app")], repository, run_id
    )

    assert report.packages[0].status is PackageStatus.RESOLVED
    assert report.packages[0].application_id == 7
    stored = repository.get_tracked_package("org.example.app")
    assert stored is not None
    assert stored.hmdm_application_id == 7


def test_missing_application_is_reported_and_id_left_null(repository: StateRepository) -> None:
    run_id = repository.start_run()
    report = reconcile([declared("org.example.app")], [], repository, run_id)

    assert report.packages[0].status is PackageStatus.NOT_IN_HEADWIND
    assert report.packages[0].application_id is None
    stored = repository.get_tracked_package("org.example.app")
    assert stored is not None
    assert stored.hmdm_application_id is None
    assert report.blocking_count == 1


def test_several_candidates_refuse_to_resolve(repository: StateRepository) -> None:
    run_id = repository.start_run()
    candidates = [
        application(7, "org.example.app"),
        application(9, "org.example.app", common=True),
    ]
    report = reconcile([declared("org.example.app")], candidates, repository, run_id)

    entry = report.packages[0]
    assert entry.status is PackageStatus.AMBIGUOUS
    assert entry.application_id is None
    assert sorted(ref.id for ref in entry.candidates) == [7, 9]
    stored = repository.get_tracked_package("org.example.app")
    assert stored is not None
    assert stored.hmdm_application_id is None


def test_ambiguity_does_not_overwrite_a_previous_resolution(
    repository: StateRepository,
) -> None:
    first_run = repository.start_run()
    reconcile(
        [declared("org.example.app")], [application(7, "org.example.app")], repository, first_run
    )

    second_run = repository.start_run()
    reconcile(
        [declared("org.example.app")],
        [application(7, "org.example.app"), application(9, "org.example.app")],
        repository,
        second_run,
    )

    stored = repository.get_tracked_package("org.example.app")
    assert stored is not None
    assert stored.hmdm_application_id is None


def test_paused_package_keeps_its_resolution(repository: StateRepository) -> None:
    run_id = repository.start_run()
    report = reconcile(
        [declared("org.example.app", paused=True)],
        [application(7, "org.example.app")],
        repository,
        run_id,
    )

    assert report.packages[0].status is PackageStatus.PAUSED
    assert report.packages[0].application_id == 7
    assert report.blocking_count == 0


def test_paused_package_never_blocks_even_when_unresolvable(
    repository: StateRepository,
) -> None:
    run_id = repository.start_run()
    report = reconcile(
        [declared("org.absent", paused=True), declared("org.ambiguous", paused=True)],
        [application(7, "org.ambiguous"), application(9, "org.ambiguous")],
        repository,
        run_id,
    )

    assert {entry.status for entry in report.packages} == {PackageStatus.PAUSED}
    assert report.blocking_count == 0
    assert repository.count_events(run_id) == 0


def test_paused_and_ambiguous_keeps_application_id_null(repository: StateRepository) -> None:
    run_id = repository.start_run()
    report = reconcile(
        [declared("org.ambiguous", paused=True)],
        [application(7, "org.ambiguous"), application(9, "org.ambiguous")],
        repository,
        run_id,
    )

    assert report.packages[0].application_id is None
    stored = repository.get_tracked_package("org.ambiguous")
    assert stored is not None
    assert stored.hmdm_application_id is None


def test_package_removed_from_file_is_dropped(repository: StateRepository) -> None:
    first_run = repository.start_run()
    reconcile(
        [declared("org.example.app"), declared("org.example.other")],
        [application(7, "org.example.app"), application(8, "org.example.other")],
        repository,
        first_run,
    )

    second_run = repository.start_run()
    report = reconcile(
        [declared("org.example.app")],
        [application(7, "org.example.app"), application(8, "org.example.other")],
        repository,
        second_run,
    )

    assert report.dropped == ["org.example.other"]
    assert repository.get_tracked_package("org.example.other") is None


def test_untracked_applications_are_listed(repository: StateRepository) -> None:
    run_id = repository.start_run()
    report = reconcile(
        [declared("org.example.app")],
        [application(7, "org.example.app"), application(8, "org.example.other")],
        repository,
        run_id,
    )

    assert [ref.id for ref in report.untracked_applications] == [8]


def test_resolution_events_are_recorded(repository: StateRepository) -> None:
    run_id = repository.start_run()
    reconcile(
        [declared("org.missing"), declared("org.ambiguous")],
        [application(7, "org.ambiguous"), application(9, "org.ambiguous")],
        repository,
        run_id,
    )

    assert repository.count_events(run_id) == 2
