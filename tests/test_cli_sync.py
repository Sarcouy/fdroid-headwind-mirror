from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from fdroid_headwind_mirror import cli
from fdroid_headwind_mirror.fdroid.client import FDroidClient
from fdroid_headwind_mirror.headwind.client import HeadwindClient
from fdroid_headwind_mirror.state.repository import StateRepository
from tests.conftest import envelope

runner = CliRunner(mix_stderr=False)

FIXTURE = Path(__file__).parent / "fixtures" / "index_extract.json"
PACKAGES = """
repo:
  url: https://f-droid.org/repo
packages:
  - pkg: org.videolan.vlc
  - pkg: com.nextcloud.client
  - pkg: com.pavelsof.wormhole
"""

APPLICATIONS = [
    {"id": 7, "name": "VLC", "pkg": "org.videolan.vlc", "version": "3.6.5", "latestVersion": 70},
    {
        "id": 8,
        "name": "Nextcloud",
        "pkg": "com.nextcloud.client",
        "version": "34.1.1",
        "latestVersion": 80,
    },
    {
        "id": 9,
        "name": "Wormhole",
        "pkg": "com.pavelsof.wormhole",
        "version": "1.0",
        "latestVersion": 90,
    },
]

VERSIONS: dict[int, list[dict[str, Any]]] = {
    7: [{"id": 70, "applicationId": 7, "version": "3.6.5", "versionCode": 13060506, "split": True}],
    8: [
        {
            "id": 80,
            "applicationId": 8,
            "version": "34.1.1",
            "versionCode": 340010190,
            "split": False,
        }
    ],
    9: [{"id": 90, "applicationId": 9, "version": "1.0", "versionCode": 1, "split": False}],
}


def fdroid_handler(request: httpx.Request) -> httpx.Response:
    index = FIXTURE.read_bytes()
    if request.url.path.endswith("/entry.json"):
        body = json.dumps(
            {
                "timestamp": 1789478586569,
                "maxAge": 14,
                "index": {
                    "name": "/index-v2.json",
                    "sha256": hashlib.sha256(index).hexdigest(),
                    "numPackages": 4,
                },
                "diffs": {},
            }
        ).encode()
        return httpx.Response(200, content=body, headers={"ETag": '"idx"'})
    if request.url.path.endswith("/index-v2.json"):
        return httpx.Response(200, content=index)
    return httpx.Response(404)


HEADWIND_CALLS: list[tuple[str, str]] = []


def headwind_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    HEADWIND_CALLS.append((request.method, path))
    if path.endswith("/applications/search"):
        return envelope(APPLICATIONS)
    for app_id, versions in VERSIONS.items():
        if path.endswith(f"/applications/{app_id}/versions"):
            return envelope(versions)
    return envelope([])


@pytest.fixture(name="workspace")
def fixture_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    HEADWIND_CALLS.clear()
    (tmp_path / "packages.yaml").write_text(PACKAGES, encoding="utf-8")
    monkeypatch.setenv("FHM_HEADWIND_URL", "https://mdm.example.org")
    monkeypatch.setenv("FHM_HEADWIND_TOKEN", "token")
    monkeypatch.setenv("FHM_PACKAGES_FILE", str(tmp_path / "packages.yaml"))
    monkeypatch.setenv("FHM_DATABASE_PATH", str(tmp_path / "state.db"))
    monkeypatch.setenv("FHM_CACHE_DIR", str(tmp_path / "cache"))

    def headwind_factory(base_url: str, token: str, timeout: float) -> HeadwindClient:
        return HeadwindClient(
            base_url=base_url,
            token=token,
            timeout=timeout,
            transport=httpx.MockTransport(headwind_handler),
        )

    def fdroid_factory(repo_url: str, timeout: float) -> FDroidClient:
        return FDroidClient(
            repo_url, timeout=timeout, transport=httpx.MockTransport(fdroid_handler)
        )

    monkeypatch.setattr(cli, "HeadwindClient", headwind_factory)
    monkeypatch.setattr(cli, "FDroidClient", fdroid_factory)
    return tmp_path


def report() -> dict[str, Any]:
    result = runner.invoke(cli.app, ["sync", "--json"])
    assert result.stdout.lstrip().startswith("{"), result.stdout
    return json.loads(result.stdout)


def by_pkg(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {entry["pkg"]: entry for entry in plan["packages"]}


@pytest.mark.usefixtures("workspace")
def test_vlc_update_uses_the_arm64_apk() -> None:
    plan = by_pkg(report())["org.videolan.vlc"]

    assert plan["status"] == "UPDATE_AVAILABLE"
    assert plan["candidate_version_code"] == 13070106
    assert plan["candidate_version"] == "3.7.1"
    assert plan["split"] is True
    assert [a["abi"] for a in plan["artifacts"]] == ["arm64-v8a"]
    assert plan["artifacts"][0]["url"].startswith("https://f-droid.org/repo/")


@pytest.mark.usefixtures("workspace")
def test_nextcloud_is_up_to_date_because_rc_is_skipped() -> None:
    plan = by_pkg(report())["com.nextcloud.client"]

    assert plan["status"] == "UP_TO_DATE"
    assert plan["candidate_version"] == "34.1.1"
    assert plan["skipped_prereleases"] == 2


@pytest.mark.usefixtures("workspace")
def test_package_without_arm64_is_rejected() -> None:
    plan = by_pkg(report())["com.pavelsof.wormhole"]

    assert plan["status"] == "REJECTED"
    assert "armeabi-v7a" in plan["detail"]


@pytest.mark.usefixtures("workspace")
def test_signer_is_pinned_on_first_run_then_matches() -> None:
    first = by_pkg(report())["org.videolan.vlc"]
    assert first["signer_state"] == "PINNED_NOW"

    second = by_pkg(report())["org.videolan.vlc"]
    assert second["signer_state"] == "MATCH"
    assert second["expected_signer"] == first["candidate_signer"]


def test_signer_mismatch_is_rejected(workspace: Path) -> None:
    report()
    with StateRepository(workspace / "state.db") as repository:
        repository.clear_expected_signer("org.videolan.vlc")
        repository.pin_expected_signer("org.videolan.vlc", "signataire-different")

    plan = by_pkg(report())["org.videolan.vlc"]
    assert plan["status"] == "REJECTED"
    assert plan["signer_state"] == "MISMATCH"


@pytest.mark.usefixtures("workspace")
def test_unreadable_headwind_versions_do_not_become_an_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failing(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/applications/search"):
            return envelope(APPLICATIONS)
        return envelope(None, status="ERROR", message="error.internal.server")

    monkeypatch.setattr(
        cli,
        "HeadwindClient",
        lambda base_url, token, timeout: HeadwindClient(
            base_url=base_url,
            token=token,
            timeout=timeout,
            transport=httpx.MockTransport(failing),
        ),
    )

    plan = by_pkg(report())["org.videolan.vlc"]

    assert plan["status"] == "SKIPPED"
    assert "illisibles" in plan["detail"]


@pytest.mark.usefixtures("workspace")
def test_dry_run_never_writes_to_headwind() -> None:
    report()

    assert HEADWIND_CALLS, "aucun appel Headwind observe"
    assert {method for method, _ in HEADWIND_CALLS} == {"GET"}


@pytest.mark.usefixtures("workspace")
def test_exit_code_reflects_rejections() -> None:
    result = runner.invoke(cli.app, ["sync"])

    assert result.exit_code == 1
    assert "REFUS" in result.stdout
    assert "Aucune ecriture effectuee" in result.stdout


@pytest.mark.usefixtures("workspace")
def test_apply_is_refused_before_iteration_four() -> None:
    result = runner.invoke(cli.app, ["sync", "--apply"])

    assert result.exit_code == 2
    assert "iteration 4" in result.stderr


def test_second_run_reuses_the_cache(workspace: Path) -> None:
    report()
    cache_file = workspace / "cache" / "index-cache.json"
    assert cache_file.is_file()

    plan = report()
    assert plan["index_source"] == "CACHE"
    assert plan["index_package_count"] == 3


def test_cache_holds_only_tracked_packages(workspace: Path) -> None:
    report()
    cached = json.loads((workspace / "cache" / "index-cache.json").read_text(encoding="utf-8"))

    assert sorted(cached["payload"]["packages"]) == [
        "com.nextcloud.client",
        "com.pavelsof.wormhole",
        "org.videolan.vlc",
    ]
    assert "com.shatteredpixel.shatteredpixeldungeon" not in cached["payload"]["packages"]
