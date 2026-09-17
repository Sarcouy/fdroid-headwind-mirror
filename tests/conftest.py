from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from fdroid_headwind_mirror.headwind.client import HeadwindClient
from fdroid_headwind_mirror.state.repository import StateRepository

Handler = Callable[[httpx.Request], httpx.Response]

LOGIN = "service"
PASSWORD = "secret"
JWT = "jeton-de-test"


def login_response(request: httpx.Request) -> httpx.Response | None:
    # Appele en tete des handlers pour que l'authentification reste hors du trafic qu'ils
    # observent: les assertions sur les methodes emises portent sur les appels metier.
    if not request.url.path.endswith("/public/jwt/login"):
        return None
    return httpx.Response(200, json={"id_token": JWT})


def envelope(data: Any, status: str = "OK", message: str | None = None) -> httpx.Response:
    return httpx.Response(
        200,
        content=json.dumps({"status": status, "message": message, "data": data}),
        headers={"Content-Type": "application/json"},
    )


def application_payload(
    application_id: int,
    pkg: str,
    name: str | None = None,
    version: str = "1.0.0",
    common: bool = False,
) -> dict[str, Any]:
    return {
        "id": application_id,
        "name": name or pkg.rsplit(".", maxsplit=1)[-1],
        "pkg": pkg,
        "version": version,
        "versionCode": 100,
        "url": f"https://mdm.example.org/files/{pkg}.apk",
        "latestVersion": application_id * 10,
        "type": "app",
        "split": False,
        "common": common,
        "customerId": 1,
        "unknownFutureField": "ignore",
    }


def track_package(
    repository: StateRepository,
    pkg: str = "org.videolan.vlc",
    *,
    auto_approve: bool = False,
    application_id: int | None = 7,
) -> None:
    repository.upsert_tracked_package(
        pkg,
        repo_url="https://f-droid.org/repo",
        auto_approve=auto_approve,
        paused=False,
        hmdm_application_id=application_id,
    )


@pytest.fixture(name="make_client")
def fixture_make_client() -> Callable[[Handler], HeadwindClient]:
    def factory(handler: Handler) -> HeadwindClient:
        def routed(request: httpx.Request) -> httpx.Response:
            return login_response(request) or handler(request)

        return HeadwindClient(
            base_url="https://mdm.example.org",
            login=LOGIN,
            password=PASSWORD,
            transport=httpx.MockTransport(routed),
        )

    return factory


@pytest.fixture(name="repository")
def fixture_repository(tmp_path: Path) -> Iterator[StateRepository]:
    with StateRepository(tmp_path / "state.db") as repository:
        yield repository
