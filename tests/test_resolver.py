from __future__ import annotations

import json
from pathlib import Path

import pytest

from fdroid_headwind_mirror.fdroid.models import Index, Package
from fdroid_headwind_mirror.fdroid.resolver import RejectionReason, resolve

FIXTURE = Path(__file__).parent / "fixtures" / "index_extract.json"
ARM64 = ["arm64-v8a"]


@pytest.fixture(name="index", scope="module")
def fixture_index() -> Index:
    return Index.model_validate(json.loads(FIXTURE.read_text(encoding="utf-8")))


def package(index: Index, pkg: str) -> Package:
    return index.packages[pkg]


def test_split_package_picks_the_arm64_apk_not_the_highest_version_code(index: Index) -> None:
    result = resolve("org.videolan.vlc", package(index, "org.videolan.vlc"), ARM64)

    assert result.candidate is not None
    assert result.candidate.version_code == 13070106
    assert result.candidate.version_name == "3.7.1"
    assert result.candidate.split is True
    assert [a.abi for a in result.candidate.artifacts] == ["arm64-v8a"]
    assert result.candidate.artifacts[0].headwind_arch == "arm64"


def test_highest_version_code_of_vlc_is_x86_and_must_not_be_chosen(index: Index) -> None:
    codes = [
        version.manifest.version_code
        for version in package(index, "org.videolan.vlc").versions.values()
    ]
    result = resolve("org.videolan.vlc", package(index, "org.videolan.vlc"), ARM64)

    assert max(codes) == 13070108
    assert result.candidate is not None
    assert result.candidate.version_code < max(codes)


def test_prerelease_is_skipped(index: Index) -> None:
    result = resolve("com.nextcloud.client", package(index, "com.nextcloud.client"), ARM64)

    assert result.candidate is not None
    assert result.candidate.version_code == 340010190
    assert result.candidate.version_name == "34.1.1"
    assert result.skipped_prereleases == 2
    assert result.candidate.split is False


def test_multi_abi_package_is_not_split(index: Index) -> None:
    result = resolve(
        "com.shatteredpixel.shatteredpixeldungeon",
        package(index, "com.shatteredpixel.shatteredpixeldungeon"),
        ARM64,
    )

    assert result.candidate is not None
    assert result.candidate.split is False
    assert result.candidate.artifacts[0].headwind_arch == "arm64"


def test_package_without_target_abi_is_rejected(index: Index) -> None:
    result = resolve("com.pavelsof.wormhole", package(index, "com.pavelsof.wormhole"), ARM64)

    assert result.candidate is None
    assert result.rejection is not None
    assert result.rejection.reason is RejectionReason.NO_TARGET_ABI
    assert "armeabi-v7a" in result.rejection.detail


def test_explicit_fallback_makes_the_package_resolvable(index: Index) -> None:
    result = resolve(
        "com.pavelsof.wormhole",
        package(index, "com.pavelsof.wormhole"),
        ["arm64-v8a", "armeabi-v7a"],
    )

    assert result.candidate is not None
    assert [a.abi for a in result.candidate.artifacts] == ["armeabi-v7a"]
    assert result.candidate.artifacts[0].headwind_arch == "armeabi"


def test_apk_url_is_built_from_repo_url(index: Index) -> None:
    result = resolve("org.videolan.vlc", package(index, "org.videolan.vlc"), ARM64)

    assert result.candidate is not None
    url = result.candidate.artifacts[0].url("https://f-droid.org/repo/")
    assert url.startswith("https://f-droid.org/repo/")
    assert url.endswith(".apk")
    assert "//org.videolan" not in url.removeprefix("https://")


def test_blocked_anti_feature_rejects_package() -> None:
    raw = {
        "versions": {
            "h1": {
                "file": {"name": "/a.apk", "sha256": "abc"},
                "manifest": {
                    "versionCode": 2,
                    "versionName": "1.0",
                    "signer": {"sha256": ["s1"]},
                },
                "antiFeatures": {"NonFreeNet": {}},
            }
        }
    }
    result = resolve("org.a", Package.model_validate(raw), ARM64, ["NonFreeNet"])

    assert result.rejection is not None
    assert result.rejection.reason is RejectionReason.ANTI_FEATURE


def test_multiple_signers_makes_version_unusable() -> None:
    raw = {
        "versions": {
            "h1": {
                "file": {"name": "/a.apk", "sha256": "abc"},
                "manifest": {"versionCode": 2, "signer": {"sha256": ["s1", "s2"]}},
            }
        }
    }
    result = resolve("org.a", Package.model_validate(raw), ARM64)

    assert result.rejection is not None
    assert result.rejection.reason is RejectionReason.NO_USABLE_SIGNER


def test_missing_signer_makes_version_unusable() -> None:
    raw = {
        "versions": {
            "h1": {"file": {"name": "/a.apk", "sha256": "abc"}, "manifest": {"versionCode": 2}}
        }
    }
    result = resolve("org.a", Package.model_validate(raw), ARM64)

    assert result.rejection is not None
    assert result.rejection.reason is RejectionReason.NO_USABLE_SIGNER


def test_package_with_only_prereleases_is_rejected() -> None:
    raw = {
        "versions": {
            "h1": {
                "file": {"name": "/a.apk", "sha256": "abc"},
                "manifest": {"versionCode": 2, "signer": {"sha256": ["s1"]}},
                "releaseChannels": ["Beta"],
            }
        }
    }
    result = resolve("org.a", Package.model_validate(raw), ARM64)

    assert result.rejection is not None
    assert result.rejection.reason is RejectionReason.NO_VERSION


def test_pure_java_package_supports_every_abi() -> None:
    raw = {
        "versions": {
            "h1": {
                "file": {"name": "/a.apk", "sha256": "abc"},
                "manifest": {"versionCode": 7, "signer": {"sha256": ["s1"]}},
            }
        }
    }
    result = resolve("org.a", Package.model_validate(raw), ARM64)

    assert result.candidate is not None
    assert result.candidate.split is False
    assert result.candidate.version_code == 7


def test_reference_abi_drives_the_version_code() -> None:
    def version(code: int, abi: str) -> dict[str, object]:
        return {
            "file": {"name": f"/a_{code}.apk", "sha256": f"h{code}"},
            "manifest": {
                "versionCode": code,
                "versionName": "1.0",
                "nativecode": [abi],
                "signer": {"sha256": ["s1"]},
            },
        }

    raw = {"versions": {"a": version(500, "armeabi-v7a"), "b": version(100, "arm64-v8a")}}
    result = resolve("org.a", Package.model_validate(raw), ["arm64-v8a", "armeabi-v7a"])

    assert result.candidate is not None
    assert result.candidate.version_code == 100
    assert [a.version_code for a in result.candidate.artifacts] == [100, 500]
