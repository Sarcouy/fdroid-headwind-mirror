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
from tests.conftest import envelope, login_response

runner = CliRunner(mix_stderr=False)

APK = b"APK factice pour la verification d'integrite" * 50
APK_SHA256 = hashlib.sha256(APK).hexdigest()
APK_NAME = "/org.exemple.verif_42.apk"

PACKAGES = """
repo:
  url: https://f-droid.org/repo
packages:
  - pkg: org.exemple.verif
"""

APPLICATIONS = [
    {"id": 7, "name": "App", "pkg": "org.exemple.verif", "version": "1.0", "latestVersion": 70}
]
VERSIONS = [{"id": 70, "applicationId": 7, "version": "1.0", "versionCode": 1, "split": False}]

INDEX: dict[str, Any] = {
    "repo": {"timestamp": 500},
    "packages": {
        "org.exemple.verif": {
            "metadata": {"preferredSigner": "signataire"},
            "versions": {
                APK_SHA256: {
                    "added": 1,
                    "file": {"name": APK_NAME, "sha256": APK_SHA256, "size": len(APK)},
                    "manifest": {
                        "versionCode": 42,
                        "versionName": "4.2",
                        "signer": {"sha256": ["signataire"]},
                    },
                }
            },
        }
    },
}

HEADWIND_CALLS: list[str] = []


def index_bytes() -> bytes:
    return json.dumps(INDEX).encode()


class FDroidStub:
    def __init__(self, apk_body: bytes = APK) -> None:
        self.apk_body = apk_body
        self.apk_calls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/entry.json"):
            body = json.dumps(
                {
                    "timestamp": 500,
                    "maxAge": 14,
                    "index": {
                        "name": "/index-v2.json",
                        "sha256": hashlib.sha256(index_bytes()).hexdigest(),
                    },
                    "diffs": {},
                }
            ).encode()
            return httpx.Response(200, content=body, headers={"ETag": '"e"'})
        if path.endswith("/index-v2.json"):
            return httpx.Response(200, content=index_bytes())
        if path.endswith(APK_NAME):
            self.apk_calls += 1
            return httpx.Response(200, content=self.apk_body)
        return httpx.Response(404)


def headwind_handler(request: httpx.Request) -> httpx.Response:
    login = login_response(request)
    if login is not None:
        return login
    HEADWIND_CALLS.append(request.method)
    if request.url.path.endswith("/applications/search"):
        return envelope(APPLICATIONS)
    if request.url.path.endswith("/applications/7/versions"):
        return envelope(VERSIONS)
    return envelope([])


@pytest.fixture(name="stub")
def fixture_stub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FDroidStub:
    HEADWIND_CALLS.clear()
    (tmp_path / "packages.yaml").write_text(PACKAGES, encoding="utf-8")
    monkeypatch.setenv("FHM_HEADWIND_URL", "https://mdm.example.org")
    monkeypatch.setenv("FHM_HEADWIND_LOGIN", "service")
    monkeypatch.setenv("FHM_HEADWIND_PASSWORD", "secret")
    monkeypatch.setenv("FHM_PACKAGES_FILE", str(tmp_path / "packages.yaml"))
    monkeypatch.setenv("FHM_DATABASE_PATH", str(tmp_path / "state.db"))
    monkeypatch.setenv("FHM_CACHE_DIR", str(tmp_path / "cache"))

    stub = FDroidStub()
    monkeypatch.setattr(
        cli,
        "HeadwindClient",
        lambda base_url, login, password, timeout: HeadwindClient(
            base_url=base_url,
            login=login,
            password=password,
            timeout=timeout,
            transport=httpx.MockTransport(headwind_handler),
        ),
    )
    monkeypatch.setattr(
        cli,
        "FDroidClient",
        lambda repo_url, timeout: FDroidClient(
            repo_url, timeout=timeout, transport=httpx.MockTransport(stub.handler)
        ),
    )
    return stub


def run(*args: str) -> dict[str, Any]:
    result = runner.invoke(cli.app, ["sync", "--json", *args])
    assert result.stdout.lstrip().startswith("{"), result.stdout
    return json.loads(result.stdout)


def test_plain_dry_run_does_not_download_any_apk(stub: FDroidStub) -> None:
    payload = run()

    assert stub.apk_calls == 0
    assert payload["verification"] is None
    assert payload["packages"][0]["artifacts"][0]["verified"] is None


def test_verify_apk_downloads_and_confirms_the_hash(stub: FDroidStub, tmp_path: Path) -> None:
    payload = run("--verify-apk")

    assert stub.apk_calls == 1
    artifact = payload["packages"][0]["artifacts"][0]
    assert artifact["verified"] is True
    assert artifact["reused"] is False
    assert payload["verification"]["verified"] == 1
    assert payload["verification"]["downloaded_bytes"] == len(APK)
    assert (tmp_path / "cache" / "apk" / "org.exemple.verif" / "42-arm64-v8a.apk").is_file()


def test_second_verification_reuses_the_stored_apk(stub: FDroidStub) -> None:
    run("--verify-apk")
    payload = run("--verify-apk")

    assert stub.apk_calls == 1
    assert payload["packages"][0]["artifacts"][0]["reused"] is True
    assert payload["verification"]["downloaded_bytes"] == 0
    assert payload["verification"]["reused_bytes"] == len(APK)


def test_corrupted_apk_rejects_the_package(stub: FDroidStub, tmp_path: Path) -> None:
    stub.apk_body = b"contenu falsifie"

    payload = run("--verify-apk")

    entry = payload["packages"][0]
    assert entry["status"] == "REJECTED"
    assert "sha256 hash mismatch" in entry["detail"]
    assert entry["artifacts"][0]["verified"] is False
    assert payload["verification"]["failed"] == 1
    assert not (tmp_path / "cache" / "apk" / "org.exemple.verif" / "42-arm64-v8a.apk").exists()


def test_corrupted_apk_sets_exit_code_one(stub: FDroidStub) -> None:
    stub.apk_body = b"contenu falsifie"

    result = runner.invoke(cli.app, ["sync", "--verify-apk"])

    assert result.exit_code == 1
    assert "REJECTED" in result.stdout


@pytest.mark.usefixtures("stub")
def test_verification_never_writes_to_headwind() -> None:
    run("--verify-apk")

    assert HEADWIND_CALLS
    assert set(HEADWIND_CALLS) == {"GET"}
