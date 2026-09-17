from __future__ import annotations

import hashlib
from collections.abc import Callable
from types import TracebackType
from typing import Any, Self, TypeVar

import httpx
from pydantic import TypeAdapter, ValidationError

from fdroid_headwind_mirror.headwind.errors import (
    PERMISSION_DENIED_MESSAGE,
    HeadwindApiError,
    HeadwindCredentialsError,
    HeadwindPermissionError,
    HeadwindTransportError,
)
from fdroid_headwind_mirror.headwind.models import (
    Application,
    ApplicationConfigurationLink,
    ApplicationVersion,
    NewApplicationVersion,
    ResponseStatus,
)

T = TypeVar("T")

_REST_PREFIX = "/rest"
_LOGIN_PATH = "/public/jwt/login"


def normalise_base_url(base_url: str) -> str:
    trimmed = base_url.rstrip("/")
    if trimmed.endswith(_REST_PREFIX):
        return trimmed
    return f"{trimmed}{_REST_PREFIX}"


def password_digest(password: str) -> str:
    # Headwind compare SHA1(MD5(motdepasse) + sel) au mot de passe stocke: le MD5 est impose par
    # le protocole, pas choisi pour proteger le secret. La casse compte, CryptoUtil.getHexString
    # produit des majuscules et un digest minuscule donnerait un SHA1 different, donc un 401.
    return hashlib.md5(password.encode("utf-8"), usedforsecurity=False).hexdigest().upper()


class HeadwindClient:
    def __init__(
        self,
        base_url: str,
        login: str,
        password: str,
        *,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = normalise_base_url(base_url)
        self._login = login
        # Seule l'empreinte est conservee: le mot de passe en clair ne survit pas au constructeur
        # et ne peut donc pas fuir dans une trace ou un repr d'instance.
        self._digest = password_digest(password)
        self._authenticated = False
        self._client = httpx.Client(
            base_url=self._base_url,
            headers={"Accept": "application/json"},
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

    def create_application_version(
        self, version: NewApplicationVersion
    ) -> ApplicationVersion | None:
        # None distingue "creee mais reponse inexploitable" de "refusee": _put leve deja pour une
        # enveloppe en erreur, donc arriver ici signifie que Headwind a accepte l'ecriture. Le
        # rendre indistinct d'un refus ferait republier la version au run suivant.
        path = "/private/applications/versions"
        payload = self._put(path, version.payload())
        try:
            return self._parse(ApplicationVersion, payload, path)
        except HeadwindApiError:
            return None

    def get_version_configurations(self, version_id: int) -> list[dict[str, Any]]:
        # Seule lecture rendue brute plutot que typee: l'API exige que ces entrees lui soient
        # reemises telles quelles. Les passer par les modeles supprimerait les champs qu'ils ne
        # declarent pas (extra="ignore") et convertirait versionText, entier cote serveur, en
        # chaine, ce que sa deserialisation refuse.
        path = f"/private/applications/version/{version_id}/configurations"
        payload = self._get(path)
        if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
            raise HeadwindApiError(ResponseStatus.OK, "liste de configurations attendue", path)
        return payload

    def link_version_configurations(
        self, version_id: int, configurations: list[dict[str, Any]]
    ) -> None:
        path = "/private/applications/version/configurations"
        self._post(path, {"applicationVersionId": version_id, "configurations": configurations})

    def _get(self, path: str) -> Any:
        return self._send(path, lambda: self._client.get(path))

    def _put(self, path: str, body: dict[str, object]) -> Any:
        return self._send(path, lambda: self._client.put(path, json=body))

    def _post(self, path: str, body: dict[str, object]) -> Any:
        return self._send(path, lambda: self._client.post(path, json=body))

    def _authenticate(self) -> None:
        # Le jeton vaut 24 h par defaut (jwt.validity) la ou un run dure quelques minutes: une
        # seule authentification par client suffit, sans renouvellement en cours de route.
        if self._authenticated:
            return

        try:
            response = self._client.post(
                _LOGIN_PATH, json={"login": self._login, "password": self._digest}
            )
        except httpx.HTTPError as exc:
            raise HeadwindTransportError(f"{_LOGIN_PATH}: {exc}") from exc

        if response.status_code == httpx.codes.UNAUTHORIZED:
            raise HeadwindCredentialsError(self._login)
        if response.status_code >= 400:
            raise HeadwindTransportError(f"{_LOGIN_PATH}: HTTP {response.status_code}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise HeadwindTransportError(f"{_LOGIN_PATH}: reponse non JSON") from exc

        token = payload.get("id_token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            raise HeadwindTransportError(f"{_LOGIN_PATH}: jeton absent de la reponse")

        self._client.headers["Authorization"] = f"Bearer {token}"
        self._authenticated = True

    def _send(self, path: str, call: Callable[[], httpx.Response]) -> Any:
        self._authenticate()

        try:
            response = call()
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
