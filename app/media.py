"""Filesystem browsing and ffprobe-based media inspection.

All paths are constrained to ``settings.media_root`` to prevent traversal
outside the mounted share.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .config import settings


class PathOutsideRootError(ValueError):
    """Raised when a requested path escapes the configured media root."""


def safe_path(raw: str | Path) -> Path:
    """Resolve ``raw`` and guarantee it stays within the media root."""
    root = settings.media_root.resolve()
    candidate = (root / Path(raw)).resolve() if not Path(raw).is_absolute() else Path(raw).resolve()
    if candidate != root and root not in candidate.parents:
        raise PathOutsideRootError(f"{candidate} is outside {root}")
    return candidate


def rel_to_root(path: Path) -> str:
    root = settings.media_root.resolve()
    try:
        return str(path.resolve().relative_to(root))
    except ValueError:
        return str(path)


@dataclass
class Entry:
    name: str
    rel_path: str
    is_dir: bool
    size: int = 0
    # Populated lazily / on demand for files:
    codec: Optional[str] = None
    duration: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None
    already_hevc: bool = False


@dataclass
class ProbeResult:
    codec: str
    duration: float
    width: int
    height: int
    size: int


def remove_within_root(raw: str | Path) -> None:
    """Delete a single file, but only if it lives inside the media root."""
    target = safe_path(raw)
    if target == settings.media_root.resolve():
        raise PathOutsideRootError("refusing to delete the media root")
    if target.is_dir():
        raise IsADirectoryError(target)
    target.unlink()


def is_video(path: Path) -> bool:
    return path.suffix.lower() in settings.video_extensions


def is_converted_output(path: Path) -> bool:
    """Heuristic: does this file already carry our output suffix?"""
    suffix = settings.output_suffix
    return bool(suffix) and path.stem.endswith(suffix)


def list_dir(rel: str = "") -> tuple[str, list[Entry]]:
    """List a directory under the media root, dirs first then video files."""
    target = safe_path(rel) if rel else settings.media_root.resolve()
    if not target.exists():
        raise FileNotFoundError(target)

    dirs: list[Entry] = []
    files: list[Entry] = []
    for child in sorted(target.iterdir(), key=lambda p: p.name.lower()):
        if child.name.startswith("."):
            continue
        if child.is_dir():
            dirs.append(Entry(child.name, rel_to_root(child), True))
        elif is_video(child):
            try:
                size = child.stat().st_size
            except OSError:
                size = 0
            files.append(
                Entry(
                    child.name,
                    rel_to_root(child),
                    False,
                    size=size,
                    already_hevc=is_converted_output(child),
                )
            )
    return rel_to_root(target), dirs + files


def probe(path: Path) -> ProbeResult:
    """Run ffprobe and extract the primary video stream's properties."""
    cmd = [
        settings.ffprobe,
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {path}: {out.stderr.strip()}")

    data = json.loads(out.stdout or "{}")
    streams = data.get("streams", [])
    fmt = data.get("format", {})

    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    duration = 0.0
    for source in (video.get("duration"), fmt.get("duration")):
        try:
            duration = float(source)
            if duration > 0:
                break
        except (TypeError, ValueError):
            continue

    try:
        size = int(fmt.get("size") or path.stat().st_size)
    except (TypeError, ValueError, OSError):
        size = 0

    return ProbeResult(
        codec=video.get("codec_name", "") or "",
        duration=duration,
        width=int(video.get("width") or 0),
        height=int(video.get("height") or 0),
        size=size,
    )


def probe_entry(entry: Entry) -> Entry:
    """Enrich a file Entry with codec/resolution info (best effort)."""
    try:
        result = probe(safe_path(entry.rel_path))
    except Exception:
        return entry
    entry.codec = result.codec
    entry.duration = result.duration
    entry.width = result.width
    entry.height = result.height
    entry.already_hevc = entry.already_hevc or result.codec in {"hevc", "h265"}
    return entry
