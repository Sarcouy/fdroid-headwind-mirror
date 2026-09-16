from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from fdroid_headwind_mirror.fdroid.models import HEADWIND_ARCH_BY_ABI, Package, Version


class RejectionReason(StrEnum):
    NO_VERSION = "NO_VERSION"
    NO_TARGET_ABI = "NO_TARGET_ABI"
    NO_USABLE_SIGNER = "NO_USABLE_SIGNER"
    ANTI_FEATURE = "ANTI_FEATURE"


class AbiArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    abi: str
    headwind_arch: str
    version_code: int
    version_name: str | None
    apk_path: str
    sha256: str
    size: int | None

    def url(self, repo_url: str) -> str:
        return f"{repo_url.rstrip('/')}/{self.apk_path.lstrip('/')}"


class Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    pkg: str
    version_code: int
    version_name: str | None
    signer: str
    split: bool
    artifacts: list[AbiArtifact]


class Rejection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    pkg: str
    reason: RejectionReason
    detail: str


class Resolution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate: Candidate | None = None
    rejection: Rejection | None = None
    skipped_prereleases: int = 0
    available_abis: list[str] = Field(default_factory=list)


def resolve(
    pkg: str,
    package: Package,
    target_abis: list[str],
    blocked_anti_features: list[str] | None = None,
) -> Resolution:
    blocked = set(blocked_anti_features or [])
    versions = list(package.versions.values())

    stable = [version for version in versions if not version.is_prerelease]
    skipped = len(versions) - len(stable)

    if not stable:
        return _rejected(
            pkg, RejectionReason.NO_VERSION, "aucune version stable publiee", skipped, []
        )

    allowed = [version for version in stable if not blocked & set(version.anti_features)]
    available = sorted({abi for v in allowed for abi in (v.manifest.nativecode or [])})

    if not allowed:
        return _rejected(
            pkg,
            RejectionReason.ANTI_FEATURE,
            "toutes les versions stables portent une anti-fonctionnalite bloquee",
            skipped,
            [],
        )

    signed = [version for version in allowed if version.signer_sha256 is not None]
    if not signed:
        return _rejected(
            pkg,
            RejectionReason.NO_USABLE_SIGNER,
            "signataire absent ou de cardinalite differente de 1",
            skipped,
            available,
        )

    selected = [(abi, _best_for_abi(signed, abi)) for abi in target_abis]
    retained = [(abi, version) for abi, version in selected if version is not None]

    if not retained:
        return _rejected(
            pkg,
            RejectionReason.NO_TARGET_ABI,
            f"aucune version pour {', '.join(target_abis)}"
            f" (disponibles: {', '.join(available) or 'aucune'})",
            skipped,
            available,
        )

    reference = retained[0][1]
    signer = reference.signer_sha256 or ""

    return Resolution(
        candidate=Candidate(
            pkg=pkg,
            version_code=reference.manifest.version_code,
            version_name=reference.manifest.version_name,
            signer=signer,
            split=reference.is_abi_specific,
            artifacts=[_artifact(abi, version) for abi, version in retained],
        ),
        skipped_prereleases=skipped,
        available_abis=available,
    )


def _best_for_abi(versions: list[Version], abi: str) -> Version | None:
    matching = [version for version in versions if version.supports(abi)]
    if not matching:
        return None
    return max(matching, key=lambda version: version.manifest.version_code)


def _artifact(abi: str, version: Version) -> AbiArtifact:
    return AbiArtifact(
        abi=abi,
        headwind_arch=HEADWIND_ARCH_BY_ABI.get(abi, abi),
        version_code=version.manifest.version_code,
        version_name=version.manifest.version_name,
        apk_path=version.file.name,
        sha256=version.file.sha256,
        size=version.file.size,
    )


def _rejected(
    pkg: str,
    reason: RejectionReason,
    detail: str,
    skipped_prereleases: int,
    available_abis: list[str],
) -> Resolution:
    return Resolution(
        rejection=Rejection(pkg=pkg, reason=reason, detail=detail),
        skipped_prereleases=skipped_prereleases,
        available_abis=available_abis,
    )
