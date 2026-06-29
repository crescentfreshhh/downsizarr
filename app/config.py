"""Runtime configuration, sourced from environment variables.

Every setting has a sane default so the app boots even with a bare container,
but in production these come from the Unraid template / docker-compose env.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _csv(name: str, default: str) -> list[str]:
    raw = os.getenv(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    media_root: Path = field(
        default_factory=lambda: Path(os.getenv("DOWNSIZARR_MEDIA_ROOT", "/media"))
    )
    data_dir: Path = field(
        default_factory=lambda: Path(os.getenv("DOWNSIZARR_DATA_DIR", "/config"))
    )
    output_suffix: str = os.getenv("DOWNSIZARR_OUTPUT_SUFFIX", ".hevc")
    output_container: str = os.getenv("DOWNSIZARR_OUTPUT_CONTAINER", "").strip()
    max_concurrent: int = int(os.getenv("DOWNSIZARR_MAX_CONCURRENT", "1"))

    default_encoder: str = os.getenv("DOWNSIZARR_DEFAULT_ENCODER", "libx265")
    default_crf: int = int(os.getenv("DOWNSIZARR_DEFAULT_CRF", "18"))
    default_preset: str = os.getenv("DOWNSIZARR_DEFAULT_PRESET", "slow")

    ffmpeg: str = os.getenv("DOWNSIZARR_FFMPEG", "ffmpeg")
    ffprobe: str = os.getenv("DOWNSIZARR_FFPROBE", "ffprobe")

    duration_tolerance: float = float(
        os.getenv("DOWNSIZARR_DURATION_TOLERANCE", "1.0")
    )
    video_extensions: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            ext.lower() if ext.startswith(".") else f".{ext.lower()}"
            for ext in _csv(
                "DOWNSIZARR_VIDEO_EXTENSIONS",
                ".mkv,.mp4,.avi,.mov,.m4v,.ts,.wmv,.flv,.mpg,.mpeg,.m2ts",
            )
        )
    )
    port: int = int(os.getenv("DOWNSIZARR_PORT", "8080"))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "downsizarr.db"

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.db_path}"


settings = Settings()
