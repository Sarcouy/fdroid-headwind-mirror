from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from fdroid_headwind_mirror.domain.planner import ArtifactPlan, PackagePlan, PlanStatus, SyncPlan
from fdroid_headwind_mirror.domain.publisher import PublicationOutcome, publish_plan
from fdroid_headwind_mirror.headwind.client import HeadwindClient
from fdroid_headwind_mirror.state.repository import StateRepository
from tests.conftest import envelope

APPLICATION_PATH = re.compile(r"/applications/(\d+)$")
CREATED_ID = 101


class FakeHeadwind:
    def __init__(self, *, latest_version: int | None = CREATED_ID, configurations: int = 0) -> None:
        self.puts: list[dict[str, Any]] = []
        self.latest_version = latest_version
        self.configurations = configurations
        self.create_fails = False
        self.create_returns_nothing = False
        self.configurations_fail = False
        self.application_fails = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "PUT" and path.endswith("/applications/versions"):
            return self._create(json.loads(request.content))
        if "/applications/configurations/" in path:
            if self.configurations_fail:
                return envelope(None, status="ERROR", message="error.internal.server")
            return envelope(
                [
                    {"configurationId": index, "action": 1, "configurationName": f"conf{index}"}
                    for index in range(self.configurations)
                ]
            )
        match = APPLICATION_PATH.search(path)
        if match:
            if self.application_fails:
                return envelope(None, status="ERROR", message="error.internal.server")
            return envelope(
                {
                    "id": int(match.group(1)),
                    "name": "VLC",
                    "pkg": "org.videolan.vlc",
                    "latestVersion": self.latest_version,
                }
            )
        return envelope([])

    def _create(self, body: dict[str, Any]) -> httpx.Response:
        self.puts.append(body)
        if self.create_fails:
            return envelope(None, status="ERROR", message="error.duplicate.application.version")
        if self.create_returns_nothing:
            return envelope(None)
        return envelope({**body, "id": CREATED_ID})


def artifact(
    abi: str = "arm64-v8a",
    arch: str = "arm64",
    url: str = "https://f-droid.org/repo/vlc-arm64.apk",
    verified: bool | None = True,
) -> ArtifactPlan:
    return ArtifactPlan(
        abi=abi,
        headwind_arch=arch,
        version_code=13070106,
        url=url,
        sha256="a" * 64,
        size=1024,
        verified=verified,
    )


def package_plan(
    *,
    split: bool = False,
    artifacts: list[ArtifactPlan] | None = None,
    status: PlanStatus = PlanStatus.UPDATE_AVAILABLE,
    application_id: int | None = 7,
) -> PackagePlan:
    return PackagePlan(
        pkg="org.videolan.vlc",
        status=status,
        application_id=application_id,
        candidate_version="3.7.1",
        candidate_version_code=13070106,
        split=split,
        artifacts=artifacts if artifacts is not None else [artifact()],
    )


def plan_of(*packages: PackagePlan) -> SyncPlan:
    return SyncPlan(
        run_id=1,
        index_source="NETWORK",
        index_timestamp=1789478586569,
        index_package_count=len(packages),
        packages=list(packages),
    )


@pytest.fixture(name="tracked")
def fixture_tracked(repository: StateRepository) -> StateRepository:
    repository.upsert_tracked_package(
        "org.videolan.vlc",
        repo_url="https://f-droid.org/repo",
        mirror=False,
        auto_approve=False,
        paused=False,
        hmdm_application_id=7,
    )
    return repository


def publish(
    plan: SyncPlan,
    server: FakeHeadwind,
    repository: StateRepository,
    make_client: Callable[[Any], HeadwindClient],
) -> Any:
    run_id = repository.start_run()
    with make_client(server.handler) as client:
        return publish_plan(plan, client, repository, run_id=run_id)


def test_non_split_version_is_created_with_a_single_url(
    tracked: StateRepository, make_client: Any
) -> None:
    server = FakeHeadwind()

    summary = publish(plan_of(package_plan()), server, tracked, make_client)

    assert server.puts == [
        {
            "applicationId": 7,
            "version": "3.7.1",
            "versionCode": 13070106,
            "split": False,
            "url": "https://f-droid.org/repo/vlc-arm64.apk",
        }
    ]
    assert summary.created == 1
    assert summary.entries[0].version_id == CREATED_ID


def test_split_version_carries_one_url_per_architecture(
    tracked: StateRepository, make_client: Any
) -> None:
    server = FakeHeadwind()
    entry = package_plan(
        split=True,
        artifacts=[
            artifact(url="https://f-droid.org/repo/vlc-arm64.apk"),
            artifact(
                abi="armeabi-v7a", arch="armeabi", url="https://f-droid.org/repo/vlc-armeabi.apk"
            ),
        ],
    )

    publish(plan_of(entry), server, tracked, make_client)

    assert server.puts[0] == {
        "applicationId": 7,
        "version": "3.7.1",
        "versionCode": 13070106,
        "split": True,
        "urlArm64": "https://f-droid.org/repo/vlc-arm64.apk",
        "urlArmeabi": "https://f-droid.org/repo/vlc-armeabi.apk",
    }


def test_created_version_is_recorded_in_the_state(
    tracked: StateRepository, make_client: Any
) -> None:
    publish(plan_of(package_plan()), FakeHeadwind(), tracked, make_client)

    stored = tracked.get_tracked_package("org.videolan.vlc")
    assert stored is not None
    assert stored.last_created_version_code == 13070106
    assert stored.awaiting_approval is True


def test_unverified_apk_is_never_published(tracked: StateRepository, make_client: Any) -> None:
    server = FakeHeadwind()
    entry = package_plan(artifacts=[artifact(verified=None)])

    summary = publish(plan_of(entry), server, tracked, make_client)

    assert not server.puts
    assert summary.entries[0].outcome is PublicationOutcome.SKIPPED
    assert "non verifie" in summary.entries[0].detail


def test_a_version_already_created_is_not_published_again(
    tracked: StateRepository, make_client: Any
) -> None:
    server = FakeHeadwind()

    publish(plan_of(package_plan()), server, tracked, make_client)
    summary = publish(plan_of(package_plan()), server, tracked, make_client)

    assert len(server.puts) == 1
    assert summary.entries[0].outcome is PublicationOutcome.SKIPPED
    assert "deja creee" in summary.entries[0].detail


def test_only_updates_are_published(tracked: StateRepository, make_client: Any) -> None:
    server = FakeHeadwind()
    plan = plan_of(
        package_plan(status=PlanStatus.UP_TO_DATE),
        package_plan(status=PlanStatus.REJECTED),
        package_plan(status=PlanStatus.SKIPPED),
    )

    summary = publish(plan, server, tracked, make_client)

    assert not server.puts
    assert not summary.entries


def test_configurations_are_counted_before_the_creation(
    tracked: StateRepository, make_client: Any
) -> None:
    server = FakeHeadwind(configurations=3)

    summary = publish(plan_of(package_plan()), server, tracked, make_client)

    assert summary.configurations == 3
    assert "3 configuration(s)" in summary.entries[0].detail


def test_unreadable_configurations_cancel_the_creation(
    tracked: StateRepository, make_client: Any
) -> None:
    server = FakeHeadwind()
    server.configurations_fail = True

    summary = publish(plan_of(package_plan()), server, tracked, make_client)

    assert not server.puts
    assert summary.entries[0].outcome is PublicationOutcome.FAILED
    assert tracked.get_tracked_package("org.videolan.vlc").last_created_version_code is None


def test_a_refused_creation_is_reported_and_leaves_the_state_untouched(
    tracked: StateRepository, make_client: Any
) -> None:
    server = FakeHeadwind()
    server.create_fails = True

    summary = publish(plan_of(package_plan()), server, tracked, make_client)

    assert summary.failed == 1
    assert tracked.get_tracked_package("org.videolan.vlc").last_created_version_code is None


def test_a_creation_without_usable_response_is_still_recorded(
    tracked: StateRepository, make_client: Any
) -> None:
    server = FakeHeadwind()
    server.create_returns_nothing = True

    summary = publish(plan_of(package_plan()), server, tracked, make_client)

    assert summary.created == 1
    assert summary.entries[0].version_id is None
    assert tracked.get_tracked_package("org.videolan.vlc").last_created_version_code == 13070106


def test_a_version_headwind_has_not_adopted_keeps_being_reported(
    tracked: StateRepository, make_client: Any
) -> None:
    server = FakeHeadwind(latest_version=70)
    entry = package_plan()
    entry.headwind_version_code = 13060506

    publish(plan_of(entry), server, tracked, make_client)
    summary = publish(plan_of(entry), server, tracked, make_client)

    assert len(server.puts) == 1
    assert "rattachement explicite requis" in summary.entries[0].detail


def test_latest_version_not_switching_is_reported(
    tracked: StateRepository, make_client: Any
) -> None:
    server = FakeHeadwind(latest_version=70)

    summary = publish(plan_of(package_plan()), server, tracked, make_client)

    assert summary.created == 1
    assert summary.entries[0].latest_version_switched is False
    assert "latestVersion inchange" in summary.entries[0].detail


def test_an_unreadable_application_does_not_undo_the_creation(
    tracked: StateRepository, make_client: Any
) -> None:
    server = FakeHeadwind()
    server.application_fails = True

    summary = publish(plan_of(package_plan()), server, tracked, make_client)

    assert summary.created == 1
    assert summary.entries[0].latest_version_switched is None
    assert tracked.get_tracked_package("org.videolan.vlc").last_created_version_code == 13070106


def test_an_unresolved_application_is_skipped(tracked: StateRepository, make_client: Any) -> None:
    server = FakeHeadwind()

    summary = publish(plan_of(package_plan(application_id=None)), server, tracked, make_client)

    assert not server.puts
    assert summary.entries[0].outcome is PublicationOutcome.SKIPPED


def test_an_architecture_without_headwind_field_is_skipped(
    tracked: StateRepository, make_client: Any
) -> None:
    server = FakeHeadwind()
    entry = package_plan(split=True, artifacts=[artifact(abi="x86_64", arch="x86_64")])

    summary = publish(plan_of(entry), server, tracked, make_client)

    assert not server.puts
    assert "x86_64" in summary.entries[0].detail
