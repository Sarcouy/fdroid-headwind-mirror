from __future__ import annotations

import json
from copy import deepcopy
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from fdroid_headwind_mirror.fdroid.client import FDroidClient
from fdroid_headwind_mirror.fdroid.merge import merge_diff
from fdroid_headwind_mirror.fdroid.models import Index

METADATA_KEYS = frozenset({"preferredSigner", "name", "lastUpdated"})
VERSION_KEYS = frozenset({"added", "file", "manifest", "antiFeatures", "releaseChannels"})


class IndexSource(StrEnum):
    CACHE = "CACHE"
    DIFF = "DIFF"
    FULL = "FULL"


class CachedIndex(BaseModel):
    model_config = ConfigDict(extra="ignore")

    timestamp: int
    etag: str | None = None
    tracked: list[str] = Field(default_factory=list)
    payload: dict[str, Any] = Field(default_factory=dict)


class IndexRefresh(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: Index
    source: IndexSource
    timestamp: int
    package_count: int


def project(payload: dict[str, Any], tracked: list[str]) -> dict[str, Any]:
    packages = payload.get("packages") or {}
    kept: dict[str, Any] = {}
    for pkg in tracked:
        if pkg not in packages:
            continue
        entry = packages[pkg]
        if entry is None:
            kept[pkg] = None
            continue
        if not isinstance(entry, dict):
            continue
        kept[pkg] = _project_package(entry)
    return {"repo": payload.get("repo") or {}, "packages": kept}


def _project_package(entry: dict[str, Any]) -> dict[str, Any]:
    projected: dict[str, Any] = {}
    if "metadata" in entry:
        projected["metadata"] = _filter_keys(entry["metadata"], METADATA_KEYS)
    if "versions" in entry:
        versions = entry["versions"]
        projected["versions"] = (
            {digest: _filter_keys(version, VERSION_KEYS) for digest, version in versions.items()}
            if isinstance(versions, dict)
            else versions
        )
    return projected


def _filter_keys(value: Any, allowed: frozenset[str]) -> Any:
    if not isinstance(value, dict):
        return value
    return {key: item for key, item in value.items() if key in allowed}


class IndexCache:
    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> CachedIndex | None:
        if not self._path.is_file():
            return None
        try:
            return CachedIndex.model_validate(json.loads(self._path.read_text(encoding="utf-8")))
        except (OSError, ValueError, ValidationError):
            return None

    def save(self, cached: CachedIndex) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(cached.model_dump_json(), encoding="utf-8")


def refresh_index(client: FDroidClient, cache: IndexCache, tracked: list[str]) -> IndexRefresh:
    wanted = sorted(set(tracked))
    cached = cache.load()
    widened = cached is not None and not set(wanted) <= set(cached.tracked)

    response = client.fetch_entry(None if widened else (cached.etag if cached else None))

    if response.unchanged and cached is not None and not widened:
        return _refresh(cached.payload, IndexSource.CACHE, cached.timestamp)

    entry = response.entry
    if entry is None:
        if cached is None:
            raise RuntimeError("entry.json inchange sans cache disponible")
        return _refresh(cached.payload, IndexSource.CACHE, cached.timestamp)

    if cached is not None and cached.timestamp == entry.timestamp and not widened:
        cache.save(
            CachedIndex(
                timestamp=cached.timestamp,
                etag=response.etag,
                tracked=wanted,
                payload=cached.payload,
            )
        )
        return _refresh(cached.payload, IndexSource.CACHE, cached.timestamp)

    diff_file = None if widened or cached is None else entry.diffs.get(str(cached.timestamp))

    if diff_file is not None and cached is not None:
        payload = merge_diff(
            deepcopy(cached.payload), project(client.fetch_json(diff_file), wanted)
        )
        source = IndexSource.DIFF
    else:
        payload = project(client.fetch_json(entry.index), wanted)
        source = IndexSource.FULL

    cache.save(
        CachedIndex(timestamp=entry.timestamp, etag=response.etag, tracked=wanted, payload=payload)
    )
    return _refresh(payload, source, entry.timestamp)


def _refresh(payload: dict[str, Any], source: IndexSource, timestamp: int) -> IndexRefresh:
    index = Index.model_validate(payload)
    return IndexRefresh(
        index=index, source=source, timestamp=timestamp, package_count=len(index.packages)
    )
