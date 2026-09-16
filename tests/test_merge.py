from __future__ import annotations

from typing import Any

from fdroid_headwind_mirror.fdroid.merge import merge_diff


def test_adds_new_keys() -> None:
    base: dict[str, Any] = {"a": 1}
    assert merge_diff(base, {"b": 2}) == {"a": 1, "b": 2}


def test_overwrites_scalar() -> None:
    assert merge_diff({"a": 1}, {"a": 2}) == {"a": 2}


def test_null_deletes_key() -> None:
    assert merge_diff({"a": 1, "b": 2}, {"b": None}) == {"a": 1}


def test_null_on_missing_key_is_harmless() -> None:
    assert merge_diff({"a": 1}, {"absent": None}) == {"a": 1}


def test_nested_merge_keeps_untouched_siblings() -> None:
    base = {"packages": {"org.a": {"versions": {"v1": {"x": 1}, "v2": {"x": 2}}}}}
    merge_diff(base, {"packages": {"org.a": {"versions": {"v1": {"x": 9}}}}})

    assert base["packages"]["org.a"]["versions"] == {"v1": {"x": 9}, "v2": {"x": 2}}


def test_nested_null_deletes_a_single_version() -> None:
    base = {"packages": {"org.a": {"versions": {"v1": {"x": 1}, "v2": {"x": 2}}}}}
    merge_diff(base, {"packages": {"org.a": {"versions": {"v1": None}}}})

    assert base["packages"]["org.a"]["versions"] == {"v2": {"x": 2}}


def test_null_deletes_whole_package() -> None:
    base = {"packages": {"org.a": {"versions": {}}, "org.b": {"versions": {}}}}
    merge_diff(base, {"packages": {"org.a": None}})

    assert list(base["packages"]) == ["org.b"]


def test_dict_replaces_non_dict_value() -> None:
    assert merge_diff({"a": 1}, {"a": {"b": 2}}) == {"a": {"b": 2}}


def test_scalar_replaces_dict_value() -> None:
    assert merge_diff({"a": {"b": 2}}, {"a": 5}) == {"a": 5}


def test_list_is_replaced_not_concatenated() -> None:
    assert merge_diff({"abis": ["x86"]}, {"abis": ["arm64-v8a"]}) == {"abis": ["arm64-v8a"]}
