from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from fdroid_headwind_mirror.domain.linker import LinkingOutcome, LinkingSummary, link_plan
from fdroid_headwind_mirror.domain.planner import PackagePlan, PlanStatus, SyncPlan
from fdroid_headwind_mirror.headwind.client import HeadwindClient
from fdroid_headwind_mirror.state.repository import StateRepository
from tests.conftest import envelope

PKG = "org.videolan.vlc"
APPLICATION_ID = 7
VERSION_ID = 137
VERSION_CODE = 13070106
VERSIONS_PATH = re.compile(r"/applications/(\d+)/versions$")
LINKS_PATH = re.compile(r"/applications/version/(\d+)/configurations$")
APPLICATION_LINKS_PATH = re.compile(r"/applications/configurations/(\d+)$")


def candidate(configuration_id: int, action: int = 0, **extra: Any) -> dict[str, Any]:
    # Ligne de GET version/{id}/configurations: sans lien vers la version demandee, id est nul
    # et action vaut 0, le serveur ne reportant pas l'action d'une autre version.
    return {
        "id": None if action == 0 else 900 + configuration_id,
        "configurationId": configuration_id,
        "applicationId": APPLICATION_ID,
        "applicationVersionId": VERSION_ID,
        "action": action,
        "remove": action == 2,
        "showIcon": True,
        "screenOrder": 3,
        "keyCode": None,
        "bottom": False,
        "longTap": False,
        "versionText": VERSION_ID,
        **extra,
    }


def installed(configuration_id: int, action: int) -> dict[str, Any]:
    # Ligne de GET applications/configurations/{id}: un lien de l'application, toutes versions
    # confondues, ou action 0 pour une configuration qui n'en porte aucun.
    return {"configurationId": configuration_id, "applicationId": APPLICATION_ID, "action": action}


class FakeHeadwind:
    def __init__(
        self,
        links: list[dict[str, Any]] | None = None,
        application_links: list[dict[str, Any]] | None = None,
    ) -> None:
        self.posts: list[dict[str, Any]] = []
        # Par defaut, une version neuve dont la precedente est installee dans deux des trois
        # configurations.
        self.links = [candidate(1), candidate(2), candidate(3)] if links is None else links
        self.application_links = (
            [installed(1, 1), installed(2, 1), installed(3, 0)]
            if application_links is None
            else application_links
        )
        self.versions = [
            {"id": VERSION_ID, "applicationId": APPLICATION_ID, "versionCode": VERSION_CODE}
        ]
        self.post_fails = False
        self.links_fail = False
        self.application_links_fail = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/applications/version/configurations"):
            self.posts.append(json.loads(request.content))
            return self._reply(self.post_fails, None)
        if LINKS_PATH.search(path):
            return self._reply(self.links_fail, self.links)
        if APPLICATION_LINKS_PATH.search(path):
            return self._reply(self.application_links_fail, self.application_links)
        if VERSIONS_PATH.search(path):
            return envelope(self.versions)
        return envelope([])

    @staticmethod
    def _reply(failing: bool, data: Any) -> httpx.Response:
        if failing:
            return envelope(None, status="ERROR", message="error.internal.server")
        return envelope(data)


def plan_of(pkg: str = PKG, application_id: int | None = APPLICATION_ID) -> SyncPlan:
    return SyncPlan(
        run_id=1,
        index_source="CACHE",
        index_timestamp=1789478586569,
        index_package_count=1,
        packages=[
            PackagePlan(pkg=pkg, status=PlanStatus.UP_TO_DATE, application_id=application_id)
        ],
    )


def track(
    repository: StateRepository,
    *,
    auto_approve: bool = True,
    created: int | None = VERSION_CODE,
    pushed: int | None = None,
) -> None:
    repository.upsert_tracked_package(
        PKG,
        repo_url="https://f-droid.org/repo",
        auto_approve=auto_approve,
        paused=False,
        hmdm_application_id=APPLICATION_ID,
    )
    repository.set_version_progress(
        PKG, last_created_version_code=created, last_pushed_version_code=pushed
    )


def run(
    server: FakeHeadwind,
    repository: StateRepository,
    make_client: Callable[[Any], HeadwindClient],
    plan: SyncPlan | None = None,
) -> LinkingSummary:
    run_id = repository.start_run()
    with make_client(server.handler) as client:
        return link_plan(plan or plan_of(), client, repository, run_id=run_id)


def sent(server: FakeHeadwind) -> dict[int, dict[str, Any]]:
    return {item["configurationId"]: item for item in server.posts[0]["configurations"]}


def test_a_new_version_is_linked_where_the_application_is_installed(
    repository: StateRepository, make_client: Any
) -> None:
    track(repository)
    server = FakeHeadwind()

    summary = run(server, repository, make_client)

    assert len(server.posts) == 1
    assert server.posts[0]["applicationVersionId"] == VERSION_ID
    assert sorted(sent(server)) == [1, 2]
    assert [sent(server)[key]["action"] for key in (1, 2)] == [1, 1]
    assert [sent(server)[key]["notify"] for key in (1, 2)] == [True, True]
    assert summary.linked == 1
    assert summary.configurations == 2
    assert summary.entries[0].notify_requested is True


def test_entries_are_sent_back_untouched_apart_from_action_and_notify(
    repository: StateRepository, make_client: Any
) -> None:
    track(repository)
    server = FakeHeadwind(
        links=[candidate(1, champInconnuDuService="valeur", screenOrder=9)],
        application_links=[installed(1, 1)],
    )

    run(server, repository, make_client)

    item = sent(server)[1]
    assert item["champInconnuDuService"] == "valeur"
    assert item["screenOrder"] == 9
    assert item["versionText"] == VERSION_ID
    assert isinstance(item["versionText"], int)
    assert item["id"] is None
    assert item["action"] == 1
    assert item["notify"] is True


def test_a_version_already_linked_is_sent_back_without_other_configurations(
    repository: StateRepository, make_client: Any
) -> None:
    track(repository)
    server = FakeHeadwind(
        links=[candidate(1, action=1), candidate(2), candidate(3)],
        application_links=[installed(1, 1), installed(2, 0), installed(3, 0)],
    )

    summary = run(server, repository, make_client)

    assert list(sent(server)) == [1]
    assert sent(server)[1]["id"] == 901
    assert sent(server)[1]["action"] == 1
    assert summary.configurations == 1


def test_an_uninstall_requested_on_the_version_is_kept(
    repository: StateRepository, make_client: Any
) -> None:
    track(repository)
    server = FakeHeadwind(
        links=[candidate(1), candidate(2, action=2), candidate(3)],
        application_links=[installed(1, 1), installed(2, 1), installed(2, 2), installed(3, 0)],
    )

    summary = run(server, repository, make_client)

    assert sorted(sent(server)) == [1, 2]
    assert [sent(server)[key]["action"] for key in (1, 2)] == [1, 2]
    assert [sent(server)[key]["notify"] for key in (1, 2)] == [True, False]
    assert sent(server)[2]["remove"] is True
    assert summary.configurations == 1


def test_linking_records_the_pushed_version(repository: StateRepository, make_client: Any) -> None:
    track(repository)
    server = FakeHeadwind()

    run(server, repository, make_client)

    assert len(server.posts) == 1
    stored = repository.get_tracked_package(PKG)
    assert stored is not None
    assert stored.last_pushed_version_code == VERSION_CODE
    assert stored.awaiting_approval is False


def test_a_package_without_auto_approve_is_never_linked(
    repository: StateRepository, make_client: Any
) -> None:
    track(repository, auto_approve=False)
    server = FakeHeadwind()

    summary = run(server, repository, make_client)

    assert not server.posts
    assert summary.entries[0].outcome is LinkingOutcome.SKIPPED
    assert summary.awaiting_approval == [PKG]
    assert repository.get_tracked_package(PKG).last_pushed_version_code is None


def test_a_version_left_unlinked_by_a_previous_run_is_picked_up(
    repository: StateRepository, make_client: Any
) -> None:
    track(repository, created=VERSION_CODE, pushed=13060506)
    server = FakeHeadwind()

    summary = run(server, repository, make_client)

    assert len(server.posts) == 1
    assert summary.linked == 1


def test_an_already_linked_package_is_left_alone(
    repository: StateRepository, make_client: Any
) -> None:
    track(repository, pushed=VERSION_CODE)
    server = FakeHeadwind()

    summary = run(server, repository, make_client)

    assert not server.posts
    assert not summary.entries


def test_a_version_absent_from_headwind_is_reported(
    repository: StateRepository, make_client: Any
) -> None:
    track(repository)
    server = FakeHeadwind()
    server.versions = [{"id": 99, "applicationId": APPLICATION_ID, "versionCode": 1}]

    summary = run(server, repository, make_client)

    assert not server.posts
    assert summary.entries[0].outcome is LinkingOutcome.FAILED
    assert "introuvable" in summary.entries[0].detail
    assert repository.get_tracked_package(PKG).last_pushed_version_code is None


def test_no_configuration_installs_the_application(
    repository: StateRepository, make_client: Any
) -> None:
    track(repository)
    server = FakeHeadwind(application_links=[installed(1, 0), installed(2, 0), installed(3, 0)])

    summary = run(server, repository, make_client)

    assert not server.posts
    assert summary.entries[0].outcome is LinkingOutcome.SKIPPED
    assert "aucune configuration" in summary.entries[0].detail
    assert repository.get_tracked_package(PKG).last_pushed_version_code == VERSION_CODE


def test_a_refused_linking_leaves_the_state_untouched(
    repository: StateRepository, make_client: Any
) -> None:
    track(repository)
    server = FakeHeadwind()
    server.post_fails = True

    summary = run(server, repository, make_client)

    assert summary.failed == 1
    assert repository.get_tracked_package(PKG).last_pushed_version_code is None


def test_unreadable_links_are_reported(repository: StateRepository, make_client: Any) -> None:
    track(repository)
    server = FakeHeadwind()
    server.links_fail = True

    summary = run(server, repository, make_client)

    assert not server.posts
    assert summary.entries[0].outcome is LinkingOutcome.FAILED
    assert "illisibles" in summary.entries[0].detail


def test_unreadable_application_links_are_reported(
    repository: StateRepository, make_client: Any
) -> None:
    track(repository)
    server = FakeHeadwind()
    server.application_links_fail = True

    summary = run(server, repository, make_client)

    assert not server.posts
    assert summary.entries[0].outcome is LinkingOutcome.FAILED
    assert "illisibles" in summary.entries[0].detail
    assert repository.get_tracked_package(PKG).last_pushed_version_code is None


def test_an_unresolved_application_is_reported(
    repository: StateRepository, make_client: Any
) -> None:
    repository.upsert_tracked_package(
        PKG,
        repo_url="https://f-droid.org/repo",
        auto_approve=True,
        paused=False,
        hmdm_application_id=None,
    )
    repository.set_version_progress(PKG, last_created_version_code=VERSION_CODE)
    server = FakeHeadwind()

    summary = run(server, repository, make_client, plan_of(application_id=None))

    assert not server.posts
    assert summary.entries[0].outcome is LinkingOutcome.FAILED


@pytest.mark.parametrize("status", [PlanStatus.REJECTED, PlanStatus.NOT_IN_FDROID])
def test_linking_does_not_depend_on_the_plan_status(
    repository: StateRepository, make_client: Any, status: PlanStatus
) -> None:
    track(repository)
    plan = SyncPlan(
        run_id=1,
        index_source="CACHE",
        index_timestamp=1,
        index_package_count=1,
        packages=[PackagePlan(pkg=PKG, status=status, application_id=APPLICATION_ID)],
    )
    server = FakeHeadwind()

    summary = run(server, repository, make_client, plan)

    assert summary.linked == 1
