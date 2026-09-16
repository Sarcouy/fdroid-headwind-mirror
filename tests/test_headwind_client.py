from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from fdroid_headwind_mirror.headwind.client import HeadwindClient, normalise_base_url
from fdroid_headwind_mirror.headwind.errors import (
    HeadwindApiError,
    HeadwindPermissionError,
    HeadwindTransportError,
)
from tests.conftest import Handler, application_payload, envelope


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("https://mdm.example.org", "https://mdm.example.org/rest"),
        ("https://mdm.example.org/", "https://mdm.example.org/rest"),
        ("https://mdm.example.org/rest", "https://mdm.example.org/rest"),
        ("https://mdm.example.org/rest/", "https://mdm.example.org/rest"),
    ],
)
def test_normalise_base_url(given: str, expected: str) -> None:
    assert normalise_base_url(given) == expected


def test_list_applications_unwraps_envelope(
    make_client: Callable[[Handler], HeadwindClient]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/rest/private/applications/search"
        assert request.headers["Authorization"] == "Bearer token"
        return envelope([application_payload(1, "org.example.app")])

    with make_client(handler) as client:
        applications = client.list_applications()

    assert [app.pkg for app in applications] == ["org.example.app"]
    assert applications[0].latest_version == 10


def test_error_status_raises_even_on_http_200(
    make_client: Callable[[Handler], HeadwindClient]
) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return envelope(None, status="ERROR", message="error.internal.server")

    with make_client(handler) as client:
        with pytest.raises(HeadwindApiError) as excinfo:
            client.list_applications()

    assert excinfo.value.message == "error.internal.server"


def test_permission_denied_is_typed(make_client: Callable[[Handler], HeadwindClient]) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return envelope(None, status="ERROR", message="error.permission.denied")

    with make_client(handler) as client:
        with pytest.raises(HeadwindPermissionError):
            client.list_applications()


def test_http_error_is_transport_error(make_client: Callable[[Handler], HeadwindClient]) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="bad gateway")

    with make_client(handler) as client:
        with pytest.raises(HeadwindTransportError):
            client.list_applications()


def test_non_json_body_is_transport_error(make_client: Callable[[Handler], HeadwindClient]) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>login</html>")

    with make_client(handler) as client:
        with pytest.raises(HeadwindTransportError):
            client.list_applications()


def test_unreadable_payload_is_api_error(make_client: Callable[[Handler], HeadwindClient]) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return envelope([{"id": "not-an-int"}])

    with make_client(handler) as client:
        with pytest.raises(HeadwindApiError):
            client.list_applications()


def test_get_application_versions(make_client: Callable[[Handler], HeadwindClient]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/rest/private/applications/7/versions"
        return envelope(
            [
                {
                    "id": 70,
                    "applicationId": 7,
                    "version": "1.4.2",
                    "versionCode": 10402,
                    "url": "https://mdm.example.org/files/app.apk",
                    "split": False,
                }
            ]
        )

    with make_client(handler) as client:
        versions = client.get_application_versions(7)

    assert versions[0].version_code == 10402


def test_get_application_configurations(make_client: Callable[[Handler], HeadwindClient]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/rest/private/applications/configurations/7"
        return envelope(
            [
                {
                    "id": 3,
                    "configurationId": 11,
                    "configurationName": "Terrain",
                    "applicationId": 7,
                    "action": 1,
                    "outdated": True,
                },
                {"id": None, "configurationId": 12, "applicationId": 7, "action": None},
            ]
        )

    with make_client(handler) as client:
        links = client.get_application_configurations(7)

    assert [link.installs_application for link in links] == [True, False]
