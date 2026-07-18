"""ffmpeg command construction and execution with live progress.

Quality philosophy: we re-encode ONLY the video stream and copy everything
else (audio, subtitles, chapters, attachments, metadata) verbatim, so nothing
but the video codec changes. Defaults are tuned for visual transparency.
"""
from __future__ import annotations

import os
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .config import settings
from .media import ProbeResult, probe
from .models import Encoder

ProgressCallback = Callable[[float], None]

# Dotted marker inserted into in-progress output filenames (e.g.
# ``Movie.hevc.dzpart.mkv``). Kept distinctive so it won't collide with real
# media names, and recognised by :func:`app.media.is_temp_output`.
TEMP_MARKER = ".dzpart"


class TranscodeError(RuntimeError):
    pass


@dataclass
class TranscodeResult:
    output_path: Path
    output_size: int
    output_codec: str
    output_duration: float


def output_path_for(
    source: Path,
    *,
    encoder: Encoder | str | None = None,
    tag_encoder: bool = False,
    crf: int | None = None,
    preset: str | None = None,
    tag_quality: bool = False,
) -> Path:
    """Derive the destination path: same directory, identifier before extension.

    Default:                ``Movie.mkv`` -> ``Movie.hevc.mkv``
    With ``tag_encoder``:   ``Movie.hevc.nvenc.mkv``
    With ``tag_quality``:   ``Movie.hevc.crf18-slow.mkv``
    Both:                   ``Movie.hevc.nvenc.crf18-slow.mkv``
    """
    name = f"{source.stem}{settings.output_suffix}"

    if tag_encoder and encoder is not None:
        enc = encoder if isinstance(encoder, Encoder) else Encoder(encoder)
        name += f".{enc.short_tag}"
    if tag_quality and crf is not None:
        name += f".crf{crf}"
        if preset:
            name += f"-{preset}"

    ext = source.suffix
    if settings.output_container:
        ext = settings.output_container
        if not ext.startswith("."):
            ext = f".{ext}"
    return source.with_name(f"{name}{ext}")


def build_command(
    source: Path,
    dest: Path,
    *,
    encoder: Encoder,
    crf: int,
    preset: str,
    ten_bit: bool = False,
    gpu_decode: bool = False,
    metadata: dict[str, str] | None = None,
) -> list[str]:
    """Construct the ffmpeg argument list for the requested encoder.

    ``gpu_decode`` (NVENC only) enables the full CUDA pipeline: frames are
    decoded on the GPU and handed straight to NVENC without a CPU round-trip.

    ``metadata`` entries are written into the output container as global
    metadata tags (used to stamp the original file's size/name onto the
    converted file so it survives even if the DB is lost).
    """
    meta_args: list[str] = []
    for key, value in (metadata or {}).items():
        meta_args += ["-metadata", f"{key}={value}"]

    base = [settings.ffmpeg, "-y", "-hide_banner", "-nostdin"]

    # Full CUDA pipeline: decode on the GPU and keep frames in GPU memory so
    # NVENC consumes them directly. Only meaningful for the NVIDIA encoder.
    cuda_decode = gpu_decode and encoder == Encoder.HEVC_NVENC
    if cuda_decode:
        base += ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]

    # VAAPI needs the hw device declared before the input.
    if encoder == Encoder.HEVC_VAAPI:
        base += ["-vaapi_device", "/dev/dri/renderD128"]

    base += ["-i", str(source)]

    pix_fmt_10 = ["-pix_fmt", "yuv420p10le"] if ten_bit else []

    if encoder == Encoder.LIBX265:
        video = [
            "-c:v", "libx265",
            "-preset", preset,
            "-crf", str(crf),
            "-x265-params", "log-level=error",
            *pix_fmt_10,
        ]
    elif encoder == Encoder.HEVC_NVENC:
        # -cq is NVENC's quality target (analogous to CRF); -b:v 0 = pure CQ.
        video = [
            "-c:v", "hevc_nvenc",
            "-preset", preset,
            "-rc", "vbr",
            "-cq", str(crf),
            "-b:v", "0",
        ]
        if ten_bit:
            # With frames already on the GPU a CPU -pix_fmt filter can't apply,
            # so request 10-bit output via the encoder profile instead.
            video += ["-profile:v", "main10"] if cuda_decode else ["-pix_fmt", "p010le"]
    elif encoder == Encoder.HEVC_QSV:
        video = [
            "-c:v", "hevc_qsv",
            "-preset", preset,
            "-global_quality", str(crf),
            *(["-pix_fmt", "p010le"] if ten_bit else []),
        ]
    elif encoder == Encoder.HEVC_VAAPI:
        # VAAPI requires frames uploaded to the GPU first.
        fmt = "p010" if ten_bit else "nv12"
        return [
            *base,
            "-vf", f"format={fmt},hwupload",
            "-c:v", "hevc_vaapi",
            "-qp", str(crf),
            "-map", "0",
            "-c:a", "copy",
            "-c:s", "copy",
            "-map_metadata", "0",
            "-map_chapters", "0",
            *meta_args,
            "-tag:v", "hvc1",
            str(dest),
        ]
    else:  # pragma: no cover - guarded by enum
        raise TranscodeError(f"Unsupported encoder: {encoder}")

    return [
        *base,
        "-map", "0",          # take every stream from the source
        "-c", "copy",         # copy them by default...
        *video,               # ...then override the video stream
        "-map_metadata", "0",
        "-map_chapters", "0",
        *meta_args,
        "-tag:v", "hvc1",     # broad player compatibility for HEVC
        str(dest),
    ]


def _parse_progress(line: str, duration: float) -> Optional[float]:
    """Translate an ffmpeg ``-progress`` line into a 0..100 percentage."""
    if duration <= 0:
        return None
    line = line.strip()
    if line.startswith("out_time_ms="):
        try:
            micros = int(line.split("=", 1)[1])
        except ValueError:
            return None
        return max(0.0, min(100.0, (micros / 1_000_000.0) / duration * 100.0))
    if line.startswith("out_time_us="):
        try:
            micros = int(line.split("=", 1)[1])
        except ValueError:
            return None
        return max(0.0, min(100.0, (micros / 1_000_000.0) / duration * 100.0))
    return None


def transcode(
    source: Path,
    dest: Path,
    *,
    encoder: Encoder,
    crf: int,
    preset: str,
    ten_bit: bool,
    duration: float,
    gpu_decode: bool = False,
    metadata: dict[str, str] | None = None,
    on_progress: Optional[ProgressCallback] = None,
    process_sink: Optional[Callable[[subprocess.Popen], None]] = None,
) -> TranscodeResult:
    """Run ffmpeg, streaming progress, and verify the result.

    ``process_sink`` receives the Popen handle so a caller (the worker) can keep
    a reference for cancellation.
    """
    # Encode to a temporary sibling and atomically rename on success, so a
    # crash/kill mid-encode can never leave a partial file at the final path
    # (which the worker would otherwise mistake for a finished conversion). The
    # marker goes BEFORE the extension so ffmpeg still infers the container.
    tmp = dest.with_name(f"{dest.stem}{TEMP_MARKER}{dest.suffix}")
    cmd = build_command(
        source, tmp, encoder=encoder, crf=crf, preset=preset,
        ten_bit=ten_bit, gpu_decode=gpu_decode, metadata=metadata,
    )
    cmd = [*cmd[:-1], "-progress", "pipe:1", "-nostats", cmd[-1]]

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError as exc:
        _cleanup(tmp)
        raise TranscodeError(
            f"ffmpeg not found ('{settings.ffmpeg}'). Is it installed in the "
            f"container/PATH? ({exc})"
        ) from exc
    if process_sink:
        process_sink(proc)

    # Drain stderr on a separate thread so a chatty ffmpeg can never fill the
    # stderr pipe buffer and deadlock us while we're reading stdout progress.
    stderr_chunks: list[str] = []

    def _drain_stderr() -> None:
        if proc.stderr is None:
            return
        for line in proc.stderr:
            stderr_chunks.append(line)

    err_thread = threading.Thread(target=_drain_stderr, daemon=True)
    err_thread.start()

    assert proc.stdout is not None
    for line in proc.stdout:
        pct = _parse_progress(line, duration)
        if pct is not None and on_progress:
            on_progress(pct)

    proc.wait()
    err_thread.join(timeout=5)
    stderr = "".join(stderr_chunks)

    if proc.returncode != 0:
        _cleanup(tmp)
        if proc.returncode < 0:
            raise TranscodeError("Transcode canceled")
        tail = "\n".join(stderr.strip().splitlines()[-15:])
        raise TranscodeError(f"ffmpeg exited {proc.returncode}:\n{tail}")

    if not tmp.exists() or tmp.stat().st_size == 0:
        _cleanup(tmp)
        raise TranscodeError("Output file missing or empty after encode")

    # Verify the temp file before promoting it to the final destination.
    verify = _verify(source, tmp)
    os.replace(tmp, dest)

    return TranscodeResult(
        output_path=dest,
        output_size=dest.stat().st_size,
        output_codec=verify.codec,
        output_duration=verify.duration,
    )


def _verify(source: Path, dest: Path) -> ProbeResult:
    """Confirm the output is HEVC and roughly matches the source duration."""
    result = probe(dest)
    if result.codec not in {"hevc", "h265"}:
        _cleanup(dest)
        raise TranscodeError(
            f"Output codec is '{result.codec}', expected hevc"
        )
    try:
        src_dur = probe(source).duration
    except Exception:
        src_dur = 0.0
    if src_dur > 0 and result.duration > 0:
        if abs(src_dur - result.duration) > settings.duration_tolerance:
            _cleanup(dest)
            raise TranscodeError(
                f"Duration mismatch: source {src_dur:.2f}s vs "
                f"output {result.duration:.2f}s "
                f"(tolerance {settings.duration_tolerance}s)"
            )
    return result


def _cleanup(dest: Path) -> None:
    try:
        if dest.exists():
            dest.unlink()
    except OSError:
        pass


# --------------------------------------------------------------------------
# VMAF quality scoring
# --------------------------------------------------------------------------
_vmaf_available: Optional[bool] = None


def vmaf_available() -> bool:
    """Detect whether this ffmpeg build has the full libvmaf filter (cached)."""
    global _vmaf_available
    if _vmaf_available is None:
        try:
            out = subprocess.run(
                [settings.ffmpeg, "-hide_banner", "-filters"],
                capture_output=True, text=True, timeout=30,
            )
            _vmaf_available = " libvmaf " in out.stdout
        except Exception:
            _vmaf_available = False
    return _vmaf_available


def compute_vmaf(reference: Path, distorted: Path) -> Optional[float]:
    """Return the mean VMAF score (0..100) of ``distorted`` vs ``reference``.

    Returns ``None`` if libvmaf isn't available or scoring fails -- VMAF is a
    best-effort quality report and never blocks a conversion.
    """
    if not vmaf_available():
        return None

    import json
    import tempfile

    threads = settings.vmaf_threads or 0
    thread_opt = f":n_threads={threads}" if threads > 0 else ""
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        log_path = tmp.name
    try:
        # Input 0 = distorted (the HEVC output), input 1 = reference (original).
        lavfi = (
            f"[0:v][1:v]libvmaf=log_fmt=json:log_path={log_path}{thread_opt}"
        )
        proc = subprocess.run(
            [
                settings.ffmpeg, "-hide_banner", "-nostdin",
                "-i", str(distorted),
                "-i", str(reference),
                "-lavfi", lavfi,
                "-f", "null", "-",
            ],
            capture_output=True, text=True, timeout=7200,
        )
        if proc.returncode != 0:
            return None
        with open(log_path) as fh:
            data = json.load(fh)
        # Newer libvmaf schema first, then older fallbacks.
        pooled = data.get("pooled_metrics", {})
        if "vmaf" in pooled and "mean" in pooled["vmaf"]:
            return round(float(pooled["vmaf"]["mean"]), 2)
        if "VMAF score" in data:
            return round(float(data["VMAF score"]), 2)
        if "aggregate" in data and "VMAF_score" in data["aggregate"]:
            return round(float(data["aggregate"]["VMAF_score"]), 2)
        return None
    except Exception:
        return None
    finally:
        try:
            Path(log_path).unlink()
        except OSError:
            pass
