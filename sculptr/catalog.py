from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import PurePosixPath


HUNYUAN_LICENSE_URL = "https://huggingface.co/tencent/Hunyuan3D-2.1/blob/main/LICENSE"
HUNYUAN_V2_LICENSE_URL = "https://huggingface.co/tencent/Hunyuan3D-2/blob/main/LICENSE"
HUNYUAN_LICENSE_NOTICE = (
    "Review and accept the Tencent Hunyuan Community License before download. "
    "SculptR targets a US studio deployment; acceptance does not change or "
    "grant rights beyond the license."
)


@dataclass(frozen=True)
class FileRule:
    path: str
    sha256: str | None = None


@dataclass(frozen=True)
class RepositorySpec:
    repo_id: str
    revision: str
    target_prefix: str = ""
    include: tuple[str, ...] = ("*",)
    exclude: tuple[str, ...] = ()
    files: tuple[FileRule, ...] = ()
    discard_duplicate_bins: bool = False

    def accepts(self, path: str, available_paths: frozenset[str]) -> bool:
        normalized = PurePosixPath(path).as_posix()
        if normalized.startswith("/") or ".." in PurePosixPath(normalized).parts:
            return False
        if not any(fnmatchcase(normalized, pattern) for pattern in self.include):
            return False
        if any(fnmatchcase(normalized, pattern) for pattern in self.exclude):
            return False
        if self.discard_duplicate_bins and normalized.endswith(".bin"):
            if PurePosixPath(normalized).name != "pytorch_model.bin":
                safe_name = normalized.removesuffix(".bin") + ".safetensors"
                if safe_name in available_paths:
                    return False
        return True


@dataclass(frozen=True)
class ModelSpec:
    model_id: str
    name: str
    display_size: str
    role: str
    repositories: tuple[RepositorySpec, ...]
    restricted: bool = False
    license_url: str | None = None
    license_notice: str | None = None
    dependencies: tuple[str, ...] = ()


HUNYUAN_FILES = (
    FileRule("hunyuan3d-dit-v2-1/config.yaml"),
    FileRule("hunyuan3d-dit-v2-1/model.fp16.ckpt"),
    FileRule("hunyuan3d-vae-v2-1/config.yaml"),
    FileRule("hunyuan3d-vae-v2-1/model.fp16.ckpt"),
    FileRule("LICENSE"),
    FileRule("Notice.txt"),
    FileRule("README.md"),
)


CATALOG: dict[str, ModelSpec] = {
    "hunyuan": ModelSpec(
        model_id="hunyuan",
        name="Hunyuan",
        display_size="8.03 GB",
        role="Single-view shape generation with the matching Hunyuan v2.1 VAE.",
        restricted=True,
        license_url=HUNYUAN_LICENSE_URL,
        license_notice=HUNYUAN_LICENSE_NOTICE,
        repositories=(
            RepositorySpec(
                repo_id="tencent/Hunyuan3D-2.1",
                revision="0b94677654c57bb9a6b6845cd7b704ccf551d327",
                include=tuple(rule.path for rule in HUNYUAN_FILES),
                files=HUNYUAN_FILES,
            ),
        ),
    ),
    "hunyuan_mv": ModelSpec(
        model_id="hunyuan_mv",
        name="Hunyuan More Views",
        display_size="5.36 GB",
        role="Multiview Hunyuan v2.0 shape generation with its own matching VAE.",
        restricted=True,
        license_url=HUNYUAN_V2_LICENSE_URL,
        license_notice=HUNYUAN_LICENSE_NOTICE,
        repositories=(
            RepositorySpec(
                repo_id="tencent/Hunyuan3D-2mv",
                revision="3a761b539b29fe4ff64714813aa9560fd66f5de0",
                include=(
                    "hunyuan3d-dit-v2-mv/config.yaml",
                    "hunyuan3d-dit-v2-mv/model.fp16.safetensors",
                    "LICENSE",
                    "NOTICE",
                    "README.md",
                ),
                files=(
                    FileRule("hunyuan3d-dit-v2-mv/config.yaml"),
                    FileRule(
                        "hunyuan3d-dit-v2-mv/model.fp16.safetensors",
                        "d36f5881bcdc56726b73e517cd444c13c60732431622da7268145355c8d38e9c",
                    ),
                    FileRule("LICENSE"),
                    FileRule("NOTICE"),
                    FileRule("README.md"),
                ),
            ),
            RepositorySpec(
                repo_id="tencent/Hunyuan3D-2",
                revision="9cd649ba6913f7a852e3286bad86bfa9a2d83dcf",
                include=(
                    "hunyuan3d-vae-v2-0/config.yaml",
                    "hunyuan3d-vae-v2-0/model.fp16.safetensors",
                ),
                files=(
                    FileRule("hunyuan3d-vae-v2-0/config.yaml"),
                    FileRule(
                        "hunyuan3d-vae-v2-0/model.fp16.safetensors",
                        "abbad221083fbde12d7e8afc69243de7cee85abc806368ba2338fc5736be7341",
                    ),
                ),
            ),
        ),
    ),
    "hi3d": ModelSpec(
        model_id="hi3d",
        name="Hi3D for Enhance Detail",
        display_size="6.50 GB",
        role="Replacement geometry generation through the Hi3D normal bridge.",
        repositories=(
            RepositorySpec(
                repo_id="Stable-X/trellis-normal-v0-1",
                revision="fdfac1d",
                target_prefix="trellis-normal-v0-1",
            ),
            RepositorySpec(
                repo_id="Stable-X/yoso-normal-v1-8-1",
                revision="4dd7d000d76febb68254ebe27251c2cb79dc1c2a",
                target_prefix="yoso-normal-v1-8-1",
            ),
            RepositorySpec(
                repo_id="facebook/dinov2-with-registers-large",
                revision="e4c89a4e05589de9b3e188688a303d0f3c04d0f3",
                target_prefix="dinov2-with-registers-large",
                include=("README.md", "config.json", "preprocessor_config.json", "model.safetensors"),
                files=(
                    FileRule("README.md"),
                    FileRule("config.json"),
                    FileRule("preprocessor_config.json"),
                    FileRule(
                        "model.safetensors",
                        "edccedab2c4e164e80833096de89a32a6e8d7365870499a066a61dbc8894b42b",
                    ),
                ),
            ),
        ),
    ),
    "texture": ModelSpec(
        model_id="texture",
        name="Enhance Texture",
        display_size="6.89 GB",
        role="Hunyuan v2.1 PBR mesh texture repainting.",
        restricted=True,
        license_url=HUNYUAN_LICENSE_URL,
        license_notice=HUNYUAN_LICENSE_NOTICE,
        repositories=(
            RepositorySpec(
                repo_id="tencent/Hunyuan3D-2.1",
                revision="0b94677654c57bb9a6b6845cd7b704ccf551d327",
                include=(
                    "hunyuan3d-paintpbr-v2-1/*",
                    "LICENSE",
                    "Notice.txt",
                    "README.md",
                ),
                exclude=("*/training/*", "*/checkpoint-*/*"),
                discard_duplicate_bins=True,
            ),
        ),
    ),
}

MODELS = CATALOG


def get_model(model_id: str) -> ModelSpec:
    try:
        return CATALOG[model_id]
    except KeyError as error:
        raise ValueError(f"Unknown model: {model_id}") from error
