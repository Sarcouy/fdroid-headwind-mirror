from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from fdroid_headwind_mirror.headwind.client import HeadwindClient, normalise_base_url
from fdroid_headwind_mirror.headwind.errors import (
    HeadwindApiError,
    HeadwindCredentialsError,
    HeadwindPermissionError,
    HeadwindTransportError,
)
from tests.conftest import JWT, LOGIN, PASSWORD, Handler, application_payload, envelope

MD5_OF_SECRET = "5EBE2294ECD0E0F08EAB7690D2A6EE69"


def authenticating_client(handler: Handler) -> HeadwindClient:
    return HeadwindClient(
        base_url="https://mdm.example.org",
        login=LOGIN,
        password=PASSWORD,
        transport=httpx.MockTransport(handler),
    )


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
        assert request.headers["Authorization"] == f"Bearer {JWT}"
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


def test_the_password_is_sent_as_an_uppercase_md5_digest() -> None:
    bodies: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/public/jwt/login"):
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"id_token": JWT})
        return envelope([])

    with authenticating_client(handler) as client:
        client.list_applications()

    assert bodies == [{"login": LOGIN, "password": MD5_OF_SECRET}]
    assert PASSWORD not in json.dumps(bodies)


def test_the_token_is_obtained_once_and_reused() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/public/jwt/login"):
            return httpx.Response(200, json={"id_token": JWT})
        assert request.headers["Authorization"] == f"Bearer {JWT}"
        return envelope([])

    with authenticating_client(handler) as client:
        client.list_applications()
        client.list_applications()
        client.get_application_versions(7)

    assert calls.count("/rest/public/jwt/login") == 1
    assert len(calls) == 4


def test_refused_credentials_raise_a_dedicated_error() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(401)

    with authenticating_client(handler) as client:
        with pytest.raises(HeadwindCredentialsError) as excinfo:
            client.list_applications()

    assert LOGIN in str(excinfo.value)
    assert calls == ["/rest/public/jwt/login"]


def test_a_login_without_token_is_reported_as_such() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "shape"})

    with authenticating_client(handler) as client:
        with pytest.raises(HeadwindTransportError, match="token missing"):
            client.list_applications()
