from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest

from fdroid_headwind_mirror.fdroid.client import (
    FDroidClient,
    FDroidIntegrityError,
    FDroidTransportError,
)
from fdroid_headwind_mirror.fdroid.download import ApkRequest, ApkStore, fetch_apk

REPO = "https://f-droid.org/repo"
PAYLOAD = b"contenu d'apk factice" * 100
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()


class Server:
    def __init__(self, body: bytes = PAYLOAD, status: int = 200) -> None:
        self.body = body
        self.status = status
        self.calls = 0

    def handler(self, _: httpx.Request) -> httpx.Response:
        self.calls += 1
        return httpx.Response(self.status, content=self.body)


def client_for(server: Server) -> FDroidClient:
    return FDroidClient(REPO, transport=httpx.MockTransport(server.handler))


def request(size: int | None = len(PAYLOAD), digest: str = DIGEST) -> ApkRequest:
    return ApkRequest(
        pkg="org.example.app",
        version_code=42,
        abi="arm64-v8a",
        url=f"{REPO}/org.example.app_42.apk",
        expected_sha256=digest,
        expected_size=size,
    )


def test_download_stores_the_apk_and_returns_its_hash(tmp_path: Path) -> None:
    server = Server()
    store = ApkStore(tmp_path)

    with client_for(server) as client:
        result = fetch_apk(client, store, request())

    assert result.reused is False
    assert result.sha256 == DIGEST
    assert result.size == len(PAYLOAD)
    assert result.path.read_bytes() == PAYLOAD
    assert result.path == tmp_path / "org.example.app" / "42-arm64-v8a.apk"


def test_hash_mismatch_raises_and_leaves_no_file(tmp_path: Path) -> None:
    server = Server(body=b"contenu falsifie")
    store = ApkStore(tmp_path)

    with client_for(server) as client:
        with pytest.raises(FDroidIntegrityError, match="sha256 hash mismatch"):
            fetch_apk(client, store, request(size=None))

    assert not (tmp_path / "org.example.app" / "42-arm64-v8a.apk").exists()


def test_size_mismatch_raises_and_leaves_no_file(tmp_path: Path) -> None:
    shorter = PAYLOAD[:-10]
    server = Server(body=shorter)
    store = ApkStore(tmp_path)

    with client_for(server) as client:
        with pytest.raises(FDroidIntegrityError):
            fetch_apk(
                client,
                store,
                request(size=len(PAYLOAD), digest=hashlib.sha256(shorter).hexdigest()),
            )

    assert not (tmp_path / "org.example.app" / "42-arm64-v8a.apk").exists()


def test_oversized_transfer_is_interrupted(tmp_path: Path) -> None:
    server = Server(body=b"x" * 5000)
    store = ApkStore(tmp_path)

    with client_for(server) as client:
        with pytest.raises(FDroidIntegrityError, match="size above"):
            fetch_apk(client, store, request(size=100, digest="peu importe"))

    assert not (tmp_path / "org.example.app" / "42-arm64-v8a.apk").exists()


def test_http_error_leaves_no_file(tmp_path: Path) -> None:
    server = Server(status=404, body=b"")
    store = ApkStore(tmp_path)

    with client_for(server) as client:
        with pytest.raises(FDroidTransportError):
            fetch_apk(client, store, request())

    assert not (tmp_path / "org.example.app" / "42-arm64-v8a.apk").exists()


def test_valid_cached_file_is_reused_without_network(tmp_path: Path) -> None:
    server = Server()
    store = ApkStore(tmp_path)

    with client_for(server) as client:
        fetch_apk(client, store, request())
        assert server.calls == 1
        result = fetch_apk(client, store, request())

    assert result.reused is True
    assert server.calls == 1


def test_corrupted_cached_file_is_downloaded_again(tmp_path: Path) -> None:
    server = Server()
    store = ApkStore(tmp_path)
    target = tmp_path / "org.example.app" / "42-arm64-v8a.apk"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"corrompu")

    with client_for(server) as client:
        result = fetch_apk(client, store, request())

    assert result.reused is False
    assert server.calls == 1
    assert target.read_bytes() == PAYLOAD


def test_failed_download_preserves_an_existing_file(tmp_path: Path) -> None:
    """The index may republish a versionCode with a different hash (a rebuild).

    The cached file then no longer matches the expectation, but it stays valid as long as
    its replacement is not verified: a failed download must not destroy it.
    """
    store = ApkStore(tmp_path)
    target = tmp_path / "org.example.app" / "42-arm64-v8a.apk"
    target.parent.mkdir(parents=True)
    target.write_bytes(PAYLOAD)
    server = Server(body=b"reconstruction incomplete")

    with client_for(server) as client:
        with pytest.raises(FDroidIntegrityError):
            fetch_apk(client, store, request(digest="empreinte-qui-a-change", size=None))

    assert target.read_bytes() == PAYLOAD


def test_no_partial_file_is_left_behind(tmp_path: Path) -> None:
    server = Server(body=b"contenu falsifie")
    store = ApkStore(tmp_path)

    with client_for(server) as client:
        with pytest.raises(FDroidIntegrityError):
            fetch_apk(client, store, request(size=None))

    assert not list((tmp_path / "org.example.app").glob("*"))


def test_store_detects_a_file_with_the_wrong_hash(tmp_path: Path) -> None:
    store = ApkStore(tmp_path)
    path = tmp_path / "a.apk"
    path.write_bytes(PAYLOAD)

    assert store.holds(path, DIGEST) is True
    assert store.holds(path, "autre") is False
    assert store.holds(tmp_path / "absent.apk", DIGEST) is False
