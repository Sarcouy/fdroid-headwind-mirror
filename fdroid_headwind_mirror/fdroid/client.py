from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import TracebackType
from typing import Any, Self

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class FDroidError(Exception):
    pass


class FDroidTransportError(FDroidError):
    pass


class FDroidIntegrityError(FDroidError):
    pass


class EntryFile(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    sha256: str
    size: int | None = None
    num_packages: int | None = Field(default=None, alias="numPackages")


class Entry(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    timestamp: int
    version: int | None = None
    max_age: int | None = Field(default=None, alias="maxAge")
    index: EntryFile
    diffs: dict[str, EntryFile] = Field(default_factory=dict)


class EntryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entry: Entry | None
    etag: str | None

    @property
    def unchanged(self) -> bool:
        return self.entry is None


class FDroidClient:
    def __init__(
        self,
        repo_url: str,
        timeout: float = 120.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._repo_url = repo_url.rstrip("/")
        self._client = httpx.Client(
            headers={"Accept-Encoding": "gzip", "Accept": "application/json"},
            timeout=httpx.Timeout(timeout),
            transport=transport,
            follow_redirects=True,
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

    @property
    def repo_url(self) -> str:
        return self._repo_url

    def fetch_entry(self, etag: str | None = None) -> EntryResponse:
        headers = {"If-None-Match": etag} if etag else {}
        response = self._get(f"{self._repo_url}/entry.json", headers=headers)
        if response.status_code == 304:
            return EntryResponse(entry=None, etag=etag)
        try:
            entry = Entry.model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            raise FDroidIntegrityError(f"entry.json illisible: {exc}") from exc
        return EntryResponse(entry=entry, etag=response.headers.get("ETag"))

    def fetch_json(self, entry_file: EntryFile) -> dict[str, Any]:
        url = f"{self._repo_url}/{entry_file.name.lstrip('/')}"
        response = self._get(url)
        if response.status_code != 200:
            raise FDroidTransportError(f"{url}: HTTP {response.status_code}")

        digest = hashlib.sha256(response.content).hexdigest()
        if digest != entry_file.sha256:
            raise FDroidIntegrityError(
                f"{url}: empreinte sha256 divergente (attendu {entry_file.sha256}, obtenu {digest})"
            )

        try:
            payload = json.loads(response.content)
        except ValueError as exc:
            raise FDroidIntegrityError(f"{url}: JSON illisible") from exc
        if not isinstance(payload, dict):
            raise FDroidIntegrityError(f"{url}: objet JSON attendu")
        return payload

    def stream_to_file(self, url: str, destination: Path, max_bytes: int) -> tuple[int, str]:
        digest = hashlib.sha256()
        written = 0
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._client.stream("GET", url) as response:
                if response.status_code >= 400:
                    raise FDroidTransportError(f"{url}: HTTP {response.status_code}")
                with destination.open("wb") as handle:
                    for chunk in response.iter_bytes():
                        written += len(chunk)
                        if written > max_bytes:
                            raise FDroidIntegrityError(
                                f"{url}: taille superieure a {max_bytes} octets,"
                                " transfert interrompu"
                            )
                        digest.update(chunk)
                        handle.write(chunk)
        except httpx.HTTPError as exc:
            raise FDroidTransportError(f"{url}: {exc}") from exc
        return written, digest.hexdigest()

    def _get(self, url: str, headers: dict[str, str] | None = None) -> httpx.Response:
        try:
            response = self._client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            raise FDroidTransportError(f"{url}: {exc}") from exc
        if response.status_code >= 400:
            raise FDroidTransportError(f"{url}: HTTP {response.status_code}")
        return response
