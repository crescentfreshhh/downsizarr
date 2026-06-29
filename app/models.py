"""Database models and shared enums."""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"
    SKIPPED = "skipped"

    @property
    def is_terminal(self) -> bool:
        return self in {
            JobStatus.COMPLETED,
            JobStatus.FAILED,
            JobStatus.CANCELED,
            JobStatus.SKIPPED,
        }


class Encoder(str, Enum):
    LIBX265 = "libx265"          # software, best quality
    HEVC_NVENC = "hevc_nvenc"    # NVIDIA
    HEVC_QSV = "hevc_qsv"        # Intel QuickSync
    HEVC_VAAPI = "hevc_vaapi"    # VAAPI (Intel/AMD)

    @property
    def is_hardware(self) -> bool:
        return self != Encoder.LIBX265


class Batch(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    created_at: datetime = Field(default_factory=utcnow)

    # Settings snapshot for the whole batch (jobs copy these at creation time).
    encoder: str = Encoder.LIBX265.value
    crf: int = 18
    preset: str = "slow"
    ten_bit: bool = False
    compute_vmaf: bool = False
    note: str = ""


class Job(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    batch_id: Optional[int] = Field(default=None, foreign_key="batch.id", index=True)

    source_path: str = Field(index=True)
    output_path: str = ""

    status: str = Field(default=JobStatus.QUEUED.value, index=True)
    progress: float = 0.0          # 0..100
    error: str = ""

    # Encode settings (copied from batch so a job is self-describing).
    encoder: str = Encoder.LIBX265.value
    crf: int = 18
    preset: str = "slow"
    ten_bit: bool = False

    # Probed / measured metadata. NOTE: source_size (the ORIGINAL file's size in
    # bytes) is recorded before transcoding and is deliberately retained for the
    # life of the row -- even after the original file is deleted -- so it remains
    # available for later deduplication / accounting work.
    source_codec: str = ""
    source_size: int = 0           # bytes (original; kept after source deletion)
    output_size: int = 0           # bytes
    source_duration: float = 0.0   # seconds
    width: int = 0
    height: int = 0

    # Quality score (0..100) of the output vs the source, if VMAF was run.
    vmaf_score: Optional[float] = None

    source_deleted: bool = False

    created_at: datetime = Field(default_factory=utcnow)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None

    @property
    def saved_bytes(self) -> int:
        if self.status == JobStatus.COMPLETED.value and self.output_size:
            return self.source_size - self.output_size
        return 0
