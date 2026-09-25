from __future__ import annotations

import json
import os
import sys
from collections import Counter
from typing import Any

import httpx

from fdroid_headwind_mirror.headwind.client import normalise_base_url, password_digest

PAGE_SIZE = 200
MAX_PAGES = 200


def fetch_devices(base_url: str, login: str, password: str) -> list[dict[str, Any]]:
    devices: list[dict[str, Any]] = []
    with httpx.Client(
        base_url=normalise_base_url(base_url),
        headers={"Accept": "application/json"},
        timeout=httpx.Timeout(60.0),
    ) as client:
        response = client.post(
            "/public/jwt/login", json={"login": login, "password": password_digest(password)}
        )
        if response.status_code == httpx.codes.UNAUTHORIZED:
            raise SystemExit("Credentials refused by Headwind")
        response.raise_for_status()
        client.headers["Authorization"] = f"Bearer {response.json()['id_token']}"

        for page in range(1, MAX_PAGES + 1):
            response = client.post(
                "/private/devices/search", json={"pageNum": page, "pageSize": PAGE_SIZE}
            )
            response.raise_for_status()
            envelope = response.json()
            if envelope.get("status") != "OK":
                raise SystemExit(f"Headwind refused the request: {envelope.get('message')}")
            batch = _extract_items(envelope.get("data"))
            devices.extend(batch)
            if len(batch) < PAGE_SIZE:
                break
    return devices


def _extract_items(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("items", "devices", "content"):
            value = data.get(key)
            if isinstance(value, list):
                return value
    return []


def _device_info(device: dict[str, Any]) -> dict[str, Any]:
    raw = device.get("info")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def summarise(devices: list[dict[str, Any]]) -> tuple[Counter[str], Counter[str]]:
    models: Counter[str] = Counter()
    versions: Counter[str] = Counter()
    for device in devices:
        info = _device_info(device)
        models[str(info.get("model") or "inconnu")] += 1
        versions[str(info.get("androidVersion") or device.get("androidVersion") or "inconnu")] += 1
    return models, versions


def render(devices: list[dict[str, Any]]) -> None:
    models, versions = summarise(devices)
    print(f"Enrolled devices: {len(devices)}\n")

    print("Models:")
    width = max((len(name) for name in models), default=0)
    for name, count in models.most_common():
        print(f"  {name.ljust(width)}  {count}")

    print("\nAndroid versions:")
    width = max((len(name) for name in versions), default=0)
    for name, count in sorted(versions.items(), key=lambda item: item[0]):
        print(f"  {name.ljust(width)}  {count}")


def main() -> None:
    base_url = os.environ.get("FHM_HEADWIND_URL")
    login = os.environ.get("FHM_HEADWIND_LOGIN")
    password = os.environ.get("FHM_HEADWIND_PASSWORD")
    if not base_url or not login or not password:
        raise SystemExit(
            "FHM_HEADWIND_URL, FHM_HEADWIND_LOGIN and FHM_HEADWIND_PASSWORD must be set"
        )
    try:
        devices = fetch_devices(base_url, login, password)
    except httpx.HTTPError as exc:
        raise SystemExit(f"Headwind unreachable: {exc}") from exc
    render(devices)


if __name__ == "__main__":
    sys.exit(main())
