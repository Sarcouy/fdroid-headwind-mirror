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


def link(configuration_id: int, action: int | None = 1, **extra: Any) -> dict[str, Any]:
    return {
        "id": 900 + configuration_id,
        "configurationId": configuration_id,
        "applicationId": APPLICATION_ID,
        "applicationVersionId": VERSION_ID,
        "action": action,
        "showIcon": True,
        "screenOrder": 3,
        "keyCode": None,
        "bottom": False,
        "longTap": False,
        "versionText": VERSION_ID,
        **extra,
    }


class FakeHeadwind:
    def __init__(self, links: list[dict[str, Any]] | None = None) -> None:
        self.posts: list[dict[str, Any]] = []
        self.links = [link(1)] if links is None else links
        self.versions = [
            {"id": VERSION_ID, "applicationId": APPLICATION_ID, "versionCode": VERSION_CODE}
        ]
        self.post_fails = False
        self.links_fail = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/applications/version/configurations"):
            self.posts.append(json.loads(request.content))
            if self.post_fails:
                return envelope(None, status="ERROR", message="error.internal.server")
            return envelope(None)
        if LINKS_PATH.search(path):
            if self.links_fail:
                return envelope(None, status="ERROR", message="error.internal.server")
            return envelope(self.links)
        if VERSIONS_PATH.search(path):
            return envelope(self.versions)
        return envelope([])


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


def test_a_created_version_is_linked_to_its_configurations(
    repository: StateRepository, make_client: Any
) -> None:
    track(repository)
    server = FakeHeadwind(links=[link(1), link(2)])

    summary = run(server, repository, make_client)

    assert len(server.posts) == 1
    assert server.posts[0]["applicationVersionId"] == VERSION_ID
    assert [item["configurationId"] for item in server.posts[0]["configurations"]] == [1, 2]
    assert summary.linked == 1
    assert summary.configurations == 2
    assert summary.entries[0].notify_requested is True


def test_entries_are_sent_back_untouched_apart_from_notify(
    repository: StateRepository, make_client: Any
) -> None:
    track(repository)
    server = FakeHeadwind(links=[link(1, champInconnuDuService="valeur", screenOrder=9)])

    run(server, repository, make_client)

    sent = server.posts[0]["configurations"][0]
    assert sent["champInconnuDuService"] == "valeur"
    assert sent["screenOrder"] == 9
    assert sent["versionText"] == VERSION_ID
    assert isinstance(sent["versionText"], int)
    assert sent["notify"] is True


def test_action_is_never_rewritten(repository: StateRepository, make_client: Any) -> None:
    track(repository)
    server = FakeHeadwind(links=[link(1, action=1), link(2, action=2), link(3, action=0)])

    summary = run(server, repository, make_client)

    sent = {item["configurationId"]: item for item in server.posts[0]["configurations"]}
    assert [sent[key]["action"] for key in (1, 2, 3)] == [1, 2, 0]
    assert [sent[key]["notify"] for key in (1, 2, 3)] == [True, False, False]
    assert summary.configurations == 1


def test_linking_records_the_pushed_version(repository: StateRepository, make_client: Any) -> None:
    track(repository)

    run(FakeHeadwind(), repository, make_client)

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
    server = FakeHeadwind(links=[])

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
