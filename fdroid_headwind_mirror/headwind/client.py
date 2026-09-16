from __future__ import annotations

from types import TracebackType
from typing import Any, Self, TypeVar

import httpx
from pydantic import TypeAdapter, ValidationError

from fdroid_headwind_mirror.headwind.errors import (
    PERMISSION_DENIED_MESSAGE,
    HeadwindApiError,
    HeadwindPermissionError,
    HeadwindTransportError,
)
from fdroid_headwind_mirror.headwind.models import (
    Application,
    ApplicationConfigurationLink,
    ApplicationVersion,
    ResponseStatus,
)

T = TypeVar("T")

_REST_PREFIX = "/rest"


def normalise_base_url(base_url: str) -> str:
    trimmed = base_url.rstrip("/")
    if trimmed.endswith(_REST_PREFIX):
        return trimmed
    return f"{trimmed}{_REST_PREFIX}"


class HeadwindClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = normalise_base_url(base_url)
        self._client = httpx.Client(
            base_url=self._base_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
            timeout=httpx.Timeout(timeout),
            transport=transport,
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def list_applications(self) -> list[Application]:
        payload = self._get("/private/applications/search")
        return self._parse(list[Application], payload, "/private/applications/search")

    def get_application(self, application_id: int) -> Application:
        path = f"/private/applications/{application_id}"
        return self._parse(Application, self._get(path), path)

    def get_application_versions(self, application_id: int) -> list[ApplicationVersion]:
        path = f"/private/applications/{application_id}/versions"
        return self._parse(list[ApplicationVersion], self._get(path), path)

    def get_application_configurations(
        self, application_id: int
    ) -> list[ApplicationConfigurationLink]:
        path = f"/private/applications/configurations/{application_id}"
        return self._parse(list[ApplicationConfigurationLink], self._get(path), path)

    def _get(self, path: str) -> Any:
        try:
            response = self._client.get(path)
        except httpx.HTTPError as exc:
            raise HeadwindTransportError(f"{path}: {exc}") from exc

        if response.status_code >= 400:
            raise HeadwindTransportError(f"{path}: HTTP {response.status_code}")

        try:
            envelope = response.json()
        except ValueError as exc:
            raise HeadwindTransportError(f"{path}: reponse non JSON") from exc

        return self._unwrap(envelope, path)

    @staticmethod
    def _unwrap(envelope: Any, path: str) -> Any:
        if not isinstance(envelope, dict):
            raise HeadwindTransportError(f"{path}: enveloppe inattendue")

        raw_status = envelope.get("status")
        message = envelope.get("message")

        if raw_status == ResponseStatus.OK:
            return envelope.get("data")

        if message == PERMISSION_DENIED_MESSAGE:
            raise HeadwindPermissionError(str(raw_status), message, path)

        raise HeadwindApiError(str(raw_status), message, path)

    @staticmethod
    def _parse(model: type[T] | Any, payload: Any, path: str) -> T:
        if payload is None:
            raise HeadwindApiError(ResponseStatus.OK, "data absent", path)
        try:
            return TypeAdapter(model).validate_python(payload)
        except ValidationError as exc:
            raise HeadwindApiError(ResponseStatus.OK, f"reponse illisible: {exc}", path) from exc
