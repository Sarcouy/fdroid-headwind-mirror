from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from fdroid_headwind_mirror.fdroid.cache import (
    IndexCache,
    IndexSource,
    project,
    refresh_index,
)
from fdroid_headwind_mirror.fdroid.client import FDroidClient, FDroidIntegrityError

REPO = "https://f-droid.org/repo"


def version_payload(code: int, digest: str = "abc") -> dict[str, Any]:
    return {
        "added": 1,
        "file": {"name": f"/org.a_{code}.apk", "sha256": digest, "size": 10},
        "manifest": {"versionCode": code, "versionName": "1.0", "signer": {"sha256": ["s1"]}},
        "description": "champ non conserve",
    }


def index_payload(codes: list[int]) -> dict[str, Any]:
    return {
        "repo": {"timestamp": 100},
        "packages": {
            "org.a": {
                "metadata": {"preferredSigner": "s1", "screenshots": {"phone": []}},
                "versions": {f"h{code}": version_payload(code) for code in codes},
            },
            "org.autre": {"metadata": {}, "versions": {"hx": version_payload(1)}},
        },
    }


def sha(payload: dict[str, Any]) -> tuple[bytes, str]:
    body = json.dumps(payload).encode()
    return body, hashlib.sha256(body).hexdigest()


class Repo:
    def __init__(self) -> None:
        self.index = index_payload([10])
        self.diffs: dict[str, dict[str, Any]] = {}
        self.timestamp = 100
        self.etag = '"v1"'
        self.requests: list[str] = []

    def entry(self) -> dict[str, Any]:
        _, index_hash = sha(self.index)
        entry: dict[str, Any] = {
            "timestamp": self.timestamp,
            "maxAge": 14,
            "index": {"name": "/index-v2.json", "sha256": index_hash, "numPackages": 2},
            "diffs": {},
        }
        for ts, payload in self.diffs.items():
            _, digest = sha(payload)
            entry["diffs"][ts] = {"name": f"/diff/{ts}.json", "sha256": digest}
        return entry

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append(path)
        if path.endswith("/entry.json"):
            if request.headers.get("If-None-Match") == self.etag:
                return httpx.Response(304)
            body, _ = sha(self.entry())
            return httpx.Response(200, content=body, headers={"ETag": self.etag})
        if path.endswith("/index-v2.json"):
            body, _ = sha(self.index)
            return httpx.Response(200, content=body)
        for ts, payload in self.diffs.items():
            if path.endswith(f"/diff/{ts}.json"):
                body, _ = sha(payload)
                return httpx.Response(200, content=body)
        return httpx.Response(404)


@pytest.fixture(name="repo")
def fixture_repo() -> Repo:
    return Repo()


def make_client(repo: Repo) -> FDroidClient:
    return FDroidClient(REPO, transport=httpx.MockTransport(repo.handler))


def run(repo: Repo, cache: IndexCache, tracked: list[str]):  # type: ignore[no-untyped-def]
    with make_client(repo) as client:
        return refresh_index(client, cache, tracked)


def test_projection_keeps_only_tracked_packages_and_allowed_keys() -> None:
    projected = project(index_payload([10]), ["org.a"])

    assert list(projected["packages"]) == ["org.a"]
    assert set(projected["packages"]["org.a"]["metadata"]) == {"preferredSigner"}
    version = projected["packages"]["org.a"]["versions"]["h10"]
    assert set(version) == {"added", "file", "manifest"}


def test_projection_preserves_null_deletions() -> None:
    diff = {"packages": {"org.a": {"versions": {"h10": None}}, "org.b": None}}
    projected = project(diff, ["org.a", "org.b"])

    assert projected["packages"]["org.a"]["versions"]["h10"] is None
    assert projected["packages"]["org.b"] is None


def test_projection_of_diff_does_not_invent_missing_keys() -> None:
    diff = {"packages": {"org.a": {"versions": {"h11": version_payload(11)}}}}
    projected = project(diff, ["org.a"])

    assert "metadata" not in projected["packages"]["org.a"]


def test_first_run_downloads_full_index(repo: Repo, tmp_path: Path) -> None:
    result = run(repo, IndexCache(tmp_path / "index.json"), ["org.a"])

    assert result.source is IndexSource.FULL
    assert result.package_count == 1
    assert "/repo/index-v2.json" in repo.requests


def test_second_run_uses_cache_on_304(repo: Repo, tmp_path: Path) -> None:
    cache = IndexCache(tmp_path / "index.json")
    run(repo, cache, ["org.a"])
    repo.requests.clear()

    result = run(repo, cache, ["org.a"])

    assert result.source is IndexSource.CACHE
    assert repo.requests == ["/repo/entry.json"]


def test_diff_is_preferred_over_full_index(repo: Repo, tmp_path: Path) -> None:
    cache = IndexCache(tmp_path / "index.json")
    run(repo, cache, ["org.a"])

    repo.diffs["100"] = {"packages": {"org.a": {"versions": {"h11": version_payload(11)}}}}
    repo.index = index_payload([10, 11])
    repo.timestamp = 200
    repo.etag = '"v2"'
    repo.requests.clear()

    result = run(repo, cache, ["org.a"])

    assert result.source is IndexSource.DIFF
    assert "/repo/diff/100.json" in repo.requests
    assert "/repo/index-v2.json" not in repo.requests
    assert sorted(result.index.packages["org.a"].versions) == ["h10", "h11"]


def test_diff_deletion_removes_the_version_from_cache(repo: Repo, tmp_path: Path) -> None:
    cache = IndexCache(tmp_path / "index.json")
    repo.index = index_payload([10, 11])
    run(repo, cache, ["org.a"])

    repo.diffs["100"] = {"packages": {"org.a": {"versions": {"h11": None}}}}
    repo.timestamp = 200
    repo.etag = '"v2"'

    result = run(repo, cache, ["org.a"])

    assert result.source is IndexSource.DIFF
    assert list(result.index.packages["org.a"].versions) == ["h10"]


def test_diff_keeps_metadata_untouched(repo: Repo, tmp_path: Path) -> None:
    cache = IndexCache(tmp_path / "index.json")
    run(repo, cache, ["org.a"])

    repo.diffs["100"] = {"packages": {"org.a": {"versions": {"h11": version_payload(11)}}}}
    repo.timestamp = 200
    repo.etag = '"v2"'

    result = run(repo, cache, ["org.a"])

    assert result.index.packages["org.a"].metadata.preferred_signer == "s1"


def test_full_index_when_timestamp_not_in_diffs(repo: Repo, tmp_path: Path) -> None:
    cache = IndexCache(tmp_path / "index.json")
    run(repo, cache, ["org.a"])

    repo.timestamp = 999
    repo.etag = '"v2"'
    repo.requests.clear()

    result = run(repo, cache, ["org.a"])

    assert result.source is IndexSource.FULL


def test_widening_tracked_set_forces_full_refresh(repo: Repo, tmp_path: Path) -> None:
    cache = IndexCache(tmp_path / "index.json")
    run(repo, cache, ["org.a"])
    repo.requests.clear()

    result = run(repo, cache, ["org.a", "org.autre"])

    assert result.source is IndexSource.FULL
    assert "org.autre" in result.index.packages


def test_shrinking_tracked_set_keeps_the_cache(repo: Repo, tmp_path: Path) -> None:
    cache = IndexCache(tmp_path / "index.json")
    run(repo, cache, ["org.a", "org.autre"])
    repo.requests.clear()

    result = run(repo, cache, ["org.a"])

    assert result.source is IndexSource.CACHE


def test_corrupted_index_is_rejected(repo: Repo, tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/entry.json"):
            body, _ = sha(repo.entry())
            return httpx.Response(200, content=body, headers={"ETag": repo.etag})
        return httpx.Response(200, content=b'{"packages": {"falsifie": {}}}')

    with FDroidClient(REPO, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(FDroidIntegrityError, match="sha256 hash mismatch"):
            refresh_index(client, IndexCache(tmp_path / "index.json"), ["org.a"])


def test_unreadable_cache_falls_back_to_full_refresh(repo: Repo, tmp_path: Path) -> None:
    path = tmp_path / "index.json"
    path.write_text("ceci n'est pas du json", encoding="utf-8")

    result = run(repo, IndexCache(path), ["org.a"])

    assert result.source is IndexSource.FULL
