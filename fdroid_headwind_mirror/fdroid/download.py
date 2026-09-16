from __future__ import annotations

import hashlib
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from fdroid_headwind_mirror.fdroid.client import FDroidClient, FDroidIntegrityError

DEFAULT_MAX_APK_BYTES = 512 * 1024 * 1024
CHUNK = 1024 * 1024


class ApkDownload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: Path
    size: int
    sha256: str
    reused: bool


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ApkStore:
    def __init__(self, root: Path) -> None:
        self._root = root

    def path_for(self, pkg: str, version_code: int, abi: str) -> Path:
        return self._root / pkg / f"{version_code}-{abi}.apk"

    def holds(self, path: Path, expected_sha256: str) -> bool:
        return path.is_file() and file_sha256(path) == expected_sha256


class ApkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    pkg: str
    version_code: int
    abi: str
    url: str
    expected_sha256: str
    expected_size: int | None = None


def fetch_apk(
    client: FDroidClient,
    store: ApkStore,
    request: ApkRequest,
    max_bytes: int = DEFAULT_MAX_APK_BYTES,
) -> ApkDownload:
    destination = store.path_for(request.pkg, request.version_code, request.abi)

    if store.holds(destination, request.expected_sha256):
        return ApkDownload(
            path=destination,
            size=destination.stat().st_size,
            sha256=request.expected_sha256,
            reused=True,
        )

    expected_size = request.expected_size
    cap = min(expected_size, max_bytes) if expected_size is not None else max_bytes
    partial = destination.with_name(f"{destination.name}.part")
    try:
        written, digest = client.stream_to_file(request.url, partial, cap)
        if digest != request.expected_sha256:
            raise FDroidIntegrityError(
                f"{request.url}: empreinte sha256 divergente"
                f" (attendu {request.expected_sha256}, obtenu {digest})"
            )
        if expected_size is not None and written != expected_size:
            raise FDroidIntegrityError(
                f"{request.url}: taille inattendue (attendu {expected_size}, obtenu {written})"
            )
        partial.replace(destination)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise

    return ApkDownload(path=destination, size=written, sha256=digest, reused=False)
