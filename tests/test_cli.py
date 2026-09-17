from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from fdroid_headwind_mirror import cli
from fdroid_headwind_mirror.headwind.client import HeadwindClient
from tests.conftest import Handler, application_payload, envelope, login_response

runner = CliRunner(mix_stderr=False)

PACKAGES = """
repo:
  url: https://f-droid.org/repo
packages:
  - pkg: org.example.app
  - pkg: org.example.missing
"""


@pytest.fixture(name="workspace")
def fixture_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "packages.yaml").write_text(PACKAGES, encoding="utf-8")
    monkeypatch.setenv("FHM_HEADWIND_URL", "https://mdm.example.org")
    monkeypatch.setenv("FHM_HEADWIND_LOGIN", "service")
    monkeypatch.setenv("FHM_HEADWIND_PASSWORD", "secret")
    monkeypatch.setenv("FHM_PACKAGES_FILE", str(tmp_path / "packages.yaml"))
    monkeypatch.setenv("FHM_DATABASE_PATH", str(tmp_path / "state.db"))
    return tmp_path


def install_transport(monkeypatch: pytest.MonkeyPatch, handler: Handler) -> None:
    def routed(request: httpx.Request) -> httpx.Response:
        return login_response(request) or handler(request)

    def factory(base_url: str, login: str, password: str, timeout: float) -> HeadwindClient:
        return HeadwindClient(
            base_url=base_url,
            login=login,
            password=password,
            timeout=timeout,
            transport=httpx.MockTransport(routed),
        )

    monkeypatch.setattr(cli, "HeadwindClient", factory)


def serve(*payloads: dict[str, object]) -> Handler:
    def handler(_: httpx.Request) -> httpx.Response:
        return envelope(list(payloads))

    return handler


def test_status_reports_resolved_and_missing(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_transport(monkeypatch, serve(application_payload(7, "org.example.app")))

    result = runner.invoke(cli.app, ["status"])

    assert result.exit_code == 1
    assert "org.example.app" in result.stdout
    assert "ABSENT DE HEADWIND" in result.stdout
    assert (workspace / "state.db").exists()


@pytest.mark.usefixtures("workspace")
def test_status_json_output(monkeypatch: pytest.MonkeyPatch) -> None:
    install_transport(monkeypatch, serve(application_payload(7, "org.example.app")))

    result = runner.invoke(cli.app, ["status", "--json"])

    assert result.stdout.lstrip().startswith("{")
    report = json.loads(result.stdout)
    statuses = {entry["pkg"]: entry["status"] for entry in report["packages"]}
    assert statuses == {"org.example.app": "RESOLVED", "org.example.missing": "NOT_IN_HEADWIND"}


def test_status_exit_code_zero_when_everything_resolves(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (workspace / "packages.yaml").write_text(
        "repo:\n  url: https://f-droid.org/repo\npackages:\n  - pkg: org.example.app\n",
        encoding="utf-8",
    )
    install_transport(monkeypatch, serve(application_payload(7, "org.example.app")))

    result = runner.invoke(cli.app, ["status"])

    assert result.exit_code == 0


@pytest.mark.usefixtures("workspace")
def test_status_fails_cleanly_when_headwind_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="unavailable")

    install_transport(monkeypatch, handler)

    result = runner.invoke(cli.app, ["status"])

    assert result.exit_code == 2
    assert "Headwind injoignable" in result.stderr


@pytest.mark.usefixtures("workspace")
def test_status_reports_permission_denied_distinctly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return envelope(None, status="ERROR", message="error.permission.denied")

    install_transport(monkeypatch, handler)

    result = runner.invoke(cli.app, ["status"])

    assert result.exit_code == 2
    assert "Acces refuse" in result.stderr
    assert "injoignable" not in result.stderr


def test_status_fails_cleanly_on_invalid_packages_file(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (workspace / "packages.yaml").write_text("packages: []\n", encoding="utf-8")
    install_transport(monkeypatch, serve())

    result = runner.invoke(cli.app, ["status"])

    assert result.exit_code == 2
    assert "invalide" in result.stderr


@pytest.mark.usefixtures("workspace")
def test_status_reports_ambiguity(monkeypatch: pytest.MonkeyPatch) -> None:
    install_transport(
        monkeypatch,
        serve(
            application_payload(7, "org.example.app"),
            application_payload(9, "org.example.app", common=True),
        ),
    )

    result = runner.invoke(cli.app, ["status"])

    assert result.exit_code == 1
    assert "AMBIGU" in result.stdout
    assert "candidat #7" in result.stdout
    assert "candidat #9" in result.stdout
