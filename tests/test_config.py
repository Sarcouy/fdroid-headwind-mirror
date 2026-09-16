from __future__ import annotations

from pathlib import Path

import pytest

from fdroid_headwind_mirror.config import ConfigError, load_packages_file

VALID = """
repo:
  url: https://f-droid.org/repo
  fingerprint: 43238d512c1e5eb2d6569f4a3afbf5523418b82e0a3ed1552770abb9a9c9ccab

defaults:
  mirror: true
  auto_approve: false

packages:
  - pkg: org.mozilla.fennec_fdroid
    auto_approve: true
  - pkg: com.nextcloud.client
  - pkg: org.videolan.vlc
    mirror: false
    repo_url: https://apt.izzysoft.de/fdroid/repo
"""


def write(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "packages.yaml"
    path.write_text(content, encoding="utf-8")
    return path


def test_defaults_are_applied_per_package(tmp_path: Path) -> None:
    resolved = {entry.pkg: entry for entry in load_packages_file(write(tmp_path, VALID)).resolved()}

    assert resolved["org.mozilla.fennec_fdroid"].auto_approve is True
    assert resolved["org.mozilla.fennec_fdroid"].mirror is True
    assert resolved["com.nextcloud.client"].auto_approve is False
    assert resolved["org.videolan.vlc"].mirror is False


def test_repo_url_falls_back_to_global_repo(tmp_path: Path) -> None:
    resolved = {entry.pkg: entry for entry in load_packages_file(write(tmp_path, VALID)).resolved()}

    assert resolved["com.nextcloud.client"].repo_url == "https://f-droid.org/repo"
    assert resolved["org.videolan.vlc"].repo_url == "https://apt.izzysoft.de/fdroid/repo"


def test_missing_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="illisible"):
        load_packages_file(tmp_path / "absent.yaml")


def test_empty_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="vide"):
        load_packages_file(write(tmp_path, ""))


def test_invalid_yaml_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="YAML invalide"):
        load_packages_file(write(tmp_path, "repo: [unclosed"))


def test_missing_repo_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="invalide"):
        load_packages_file(write(tmp_path, "packages:\n  - pkg: org.example.app\n"))


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    content = "repo:\n  url: https://f-droid.org/repo\npackages:\n  - pkg: a\n    mirrorr: true\n"
    with pytest.raises(ConfigError, match="invalide"):
        load_packages_file(write(tmp_path, content))


def test_duplicate_package_is_rejected(tmp_path: Path) -> None:
    content = (
        "repo:\n  url: https://f-droid.org/repo\n"
        "packages:\n  - pkg: org.example.app\n  - pkg: org.example.app\n"
    )
    with pytest.raises(ConfigError, match="plusieurs fois"):
        load_packages_file(write(tmp_path, content))
