from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigError(Exception):
    pass


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FHM_", env_file=".env", extra="ignore")

    headwind_url: str
    headwind_login: str
    headwind_password: SecretStr
    packages_file: Path = Path("packages.yaml")
    database_path: Path = Path("state.db")
    cache_dir: Path = Path(".cache")
    request_timeout: float = 30.0
    fdroid_timeout: float = 300.0


class RepoConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    fingerprint: str | None = None


DEFAULT_TARGET_ABIS = ["arm64-v8a"]


class PackageDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    auto_approve: bool = False
    target_abis: list[str] = Field(default_factory=lambda: list(DEFAULT_TARGET_ABIS))
    blocked_anti_features: list[str] = Field(default_factory=list)


class TrackedPackageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pkg: str = Field(min_length=1)
    auto_approve: bool | None = None
    paused: bool = False
    repo_url: str | None = None
    target_abis: list[str] | None = Field(default=None, min_length=1)
    blocked_anti_features: list[str] | None = None


class PackagesFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo: RepoConfig
    defaults: PackageDefaults = PackageDefaults()
    packages: list[TrackedPackageConfig] = Field(default_factory=list)

    def resolved(self) -> list[ResolvedPackage]:
        return [
            ResolvedPackage(
                pkg=entry.pkg,
                repo_url=entry.repo_url or self.repo.url,
                auto_approve=(
                    entry.auto_approve
                    if entry.auto_approve is not None
                    else self.defaults.auto_approve
                ),
                paused=entry.paused,
                target_abis=entry.target_abis or self.defaults.target_abis,
                blocked_anti_features=(
                    entry.blocked_anti_features
                    if entry.blocked_anti_features is not None
                    else self.defaults.blocked_anti_features
                ),
            )
            for entry in self.packages
        ]


class ResolvedPackage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    pkg: str
    repo_url: str
    auto_approve: bool
    paused: bool
    target_abis: list[str] = Field(default_factory=lambda: list(DEFAULT_TARGET_ABIS))
    blocked_anti_features: list[str] = Field(default_factory=list)


def load_packages_file(path: Path) -> PackagesFile:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"{path}: unreadable file ({exc})") from exc

    try:
        document: Any = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML ({exc})") from exc

    if document is None:
        raise ConfigError(f"{path}: empty file")

    try:
        parsed = PackagesFile.model_validate(document)
    except ValidationError as exc:
        raise ConfigError(f"{path}: invalid configuration\n{exc}") from exc

    _reject_duplicates(parsed, path)
    return parsed


def _reject_duplicates(parsed: PackagesFile, path: Path) -> None:
    seen: set[str] = set()
    duplicates: list[str] = []
    for entry in parsed.packages:
        if entry.pkg in seen:
            duplicates.append(entry.pkg)
        seen.add(entry.pkg)
    if duplicates:
        raise ConfigError(
            f"{path}: packages declared more than once: {', '.join(sorted(duplicates))}"
        )
