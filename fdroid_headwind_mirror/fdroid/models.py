from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

ARM64 = "arm64-v8a"
ARMEABI_V7A = "armeabi-v7a"
ARMEABI = "armeabi"

HEADWIND_ARCH_BY_ABI: dict[str, str] = {
    ARM64: "arm64",
    ARMEABI_V7A: "armeabi",
    ARMEABI: "armeabi",
}


class IndexModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class FileEntry(IndexModel):
    name: str
    sha256: str
    size: int | None = None


class Signer(IndexModel):
    sha256: list[str] = Field(default_factory=list)


class Manifest(IndexModel):
    version_name: str | None = Field(default=None, alias="versionName")
    version_code: int = Field(alias="versionCode")
    nativecode: list[str] | None = None
    signer: Signer | None = None


class Version(IndexModel):
    added: int | None = None
    file: FileEntry
    manifest: Manifest
    anti_features: dict[str, dict[str, str]] = Field(default_factory=dict, alias="antiFeatures")
    release_channels: list[str] = Field(default_factory=list, alias="releaseChannels")

    @property
    def is_prerelease(self) -> bool:
        return bool(self.release_channels)

    @property
    def signer_sha256(self) -> str | None:
        if self.manifest.signer is None or len(self.manifest.signer.sha256) != 1:
            return None
        return self.manifest.signer.sha256[0]

    def supports(self, abi: str) -> bool:
        return self.manifest.nativecode is None or abi in self.manifest.nativecode

    @property
    def is_abi_specific(self) -> bool:
        return self.manifest.nativecode is not None and len(self.manifest.nativecode) == 1


class PackageMetadata(IndexModel):
    preferred_signer: str | None = Field(default=None, alias="preferredSigner")
    last_updated: int | None = Field(default=None, alias="lastUpdated")


class Package(IndexModel):
    metadata: PackageMetadata = Field(default_factory=PackageMetadata)
    versions: dict[str, Version] = Field(default_factory=dict)


class RepoInfo(IndexModel):
    timestamp: int | None = None
    address: str | None = None


class Index(IndexModel):
    repo: RepoInfo = Field(default_factory=RepoInfo)
    packages: dict[str, Package] = Field(default_factory=dict)
