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


@pytest.fixture(name="make_client")
def fixture_make_client() -> Callable[[Handler], HeadwindClient]:
    def factory(handler: Handler) -> HeadwindClient:
        return HeadwindClient(
            base_url="https://mdm.example.org",
            token="token",
            transport=httpx.MockTransport(handler),
        )

    return factory


@pytest.fixture(name="repository")
def fixture_repository(tmp_path: Path) -> Iterator[StateRepository]:
    with StateRepository(tmp_path / "state.db") as repository:
        yield repository
