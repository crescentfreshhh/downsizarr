"""Unit tests for command building, output paths, progress parsing and metrics.

These run without ffmpeg installed - they exercise pure logic only.
"""
import os
from pathlib import Path

os.environ.setdefault("DOWNSIZARR_MEDIA_ROOT", "/tmp/dz-media")
os.environ.setdefault("DOWNSIZARR_DATA_DIR", "/tmp/dz-config")

import pytest

from app import transcoder
from app.metrics import human_bytes
from app.models import Encoder


def test_output_path_default_suffix():
    src = Path("/media/Movies/Film.mkv")
    out = transcoder.output_path_for(src)
    assert out == Path("/media/Movies/Film.hevc.mkv")


def test_output_path_keeps_directory():
    src = Path("/media/TV/Show/ep01.mp4")
    out = transcoder.output_path_for(src)
    assert out.parent == src.parent
    assert out.name == "ep01.hevc.mp4"


def test_build_command_libx265_quality_flags():
    cmd = transcoder.build_command(
        Path("/in.mkv"), Path("/out.hevc.mkv"),
        encoder=Encoder.LIBX265, crf=18, preset="slow",
    )
    assert "libx265" in cmd
    # CRF value present right after -crf
    assert cmd[cmd.index("-crf") + 1] == "18"
    assert cmd[cmd.index("-preset") + 1] == "slow"
    # Everything is mapped and non-video copied verbatim.
    assert "-map" in cmd and "0" in cmd
    assert "copy" in cmd
    assert cmd[-1] == "/out.hevc.mkv"


def test_build_command_nvenc_uses_cq():
    cmd = transcoder.build_command(
        Path("/in.mkv"), Path("/out.mkv"),
        encoder=Encoder.HEVC_NVENC, crf=20, preset="p7",
    )
    assert "hevc_nvenc" in cmd
    assert cmd[cmd.index("-cq") + 1] == "20"


def test_build_command_vaapi_uploads_frames():
    cmd = transcoder.build_command(
        Path("/in.mkv"), Path("/out.mkv"),
        encoder=Encoder.HEVC_VAAPI, crf=18, preset="default",
    )
    assert "hevc_vaapi" in cmd
    assert "-vaapi_device" in cmd
    joined = " ".join(cmd)
    assert "hwupload" in joined


def test_ten_bit_flag():
    cmd = transcoder.build_command(
        Path("/in.mkv"), Path("/out.mkv"),
        encoder=Encoder.LIBX265, crf=18, preset="slow", ten_bit=True,
    )
    assert "yuv420p10le" in cmd


@pytest.mark.parametrize("line,duration,expected", [
    ("out_time_ms=30000000", 60.0, 50.0),
    ("out_time_us=60000000", 60.0, 100.0),
    ("frame=10", 60.0, None),
    ("out_time_ms=0", 0.0, None),
])
def test_progress_parsing(line, duration, expected):
    assert transcoder._parse_progress(line, duration) == expected


def test_human_bytes():
    assert human_bytes(0) == "0 B"
    assert human_bytes(1024) == "1.00 KB"
    assert human_bytes(1024 ** 3) == "1.00 GB"
    assert human_bytes(-1024 ** 3) == "-1.00 GB"
