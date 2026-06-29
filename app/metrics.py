"""Aggregate space-savings metrics for the dashboard."""
from __future__ import annotations

from dataclasses import dataclass, field

from collections import defaultdict

from sqlmodel import select

from .database import session_scope
from .models import Batch, Job, JobStatus


def human_bytes(num: int | float) -> str:
    num = float(num)
    sign = "-" if num < 0 else ""
    num = abs(num)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if num < 1024.0:
            if unit == "B":
                return f"{sign}{int(num)} {unit}"
            return f"{sign}{num:.2f} {unit}"
        num /= 1024.0
    return f"{sign}{num:.2f} EB"


@dataclass
class Stats:
    completed: int = 0
    failed: int = 0
    running: int = 0
    queued: int = 0
    skipped: int = 0
    canceled: int = 0
    total_source: int = 0
    total_output: int = 0
    points: list[tuple[str, int]] = field(default_factory=list)  # cumulative saved

    @property
    def total_saved(self) -> int:
        return self.total_source - self.total_output

    @property
    def percent_saved(self) -> float:
        if self.total_source <= 0:
            return 0.0
        return self.total_saved / self.total_source * 100.0


def collect() -> Stats:
    stats = Stats()
    with session_scope() as session:
        jobs = session.exec(select(Job).order_by(Job.finished_at)).all()
        cumulative = 0
        for job in jobs:
            status = job.status
            if status == JobStatus.COMPLETED.value:
                stats.completed += 1
                stats.total_source += job.source_size
                stats.total_output += job.output_size
                cumulative += job.source_size - job.output_size
                label = (
                    job.finished_at.strftime("%m-%d %H:%M")
                    if job.finished_at
                    else str(job.id)
                )
                stats.points.append((label, cumulative))
            elif status == JobStatus.FAILED.value:
                stats.failed += 1
            elif status == JobStatus.RUNNING.value:
                stats.running += 1
            elif status == JobStatus.QUEUED.value:
                stats.queued += 1
            elif status == JobStatus.SKIPPED.value:
                stats.skipped += 1
            elif status == JobStatus.CANCELED.value:
                stats.canceled += 1
    # Keep the sparkline manageable.
    if len(stats.points) > 60:
        stats.points = stats.points[-60:]
    return stats


@dataclass
class BatchView:
    batch: Batch
    jobs: list[Job]
    counts: dict[str, int] = field(default_factory=dict)
    total: int = 0
    total_saved: int = 0
    is_complete: bool = False        # nothing queued or running
    has_completed: bool = False      # at least one verified output exists
    deletable_count: int = 0         # completed jobs whose source is still here
    deletable_bytes: int = 0         # bytes reclaimable if those sources go
    deleted_count: int = 0           # sources already deleted


_ACTIVE = {JobStatus.QUEUED.value, JobStatus.RUNNING.value}


def batch_summaries() -> list[BatchView]:
    """One view-model per batch, newest first, for the Batches page."""
    with session_scope() as session:
        batches = session.exec(select(Batch).order_by(Batch.id.desc())).all()
        jobs = session.exec(select(Job)).all()

    grouped: dict[int, list[Job]] = defaultdict(list)
    for job in jobs:
        grouped[job.batch_id].append(job)

    views: list[BatchView] = []
    for batch in batches:
        bjobs = sorted(grouped.get(batch.id, []), key=lambda j: j.id or 0)
        counts: dict[str, int] = defaultdict(int)
        total_saved = 0
        deletable_count = deletable_bytes = deleted_count = 0
        for job in bjobs:
            counts[job.status] += 1
            if job.status == JobStatus.COMPLETED.value:
                total_saved += job.saved_bytes
                if job.source_deleted:
                    deleted_count += 1
                else:
                    deletable_count += 1
                    deletable_bytes += job.source_size
        views.append(
            BatchView(
                batch=batch,
                jobs=bjobs,
                counts=dict(counts),
                total=len(bjobs),
                total_saved=total_saved,
                is_complete=bool(bjobs)
                and not any(j.status in _ACTIVE for j in bjobs),
                has_completed=counts.get(JobStatus.COMPLETED.value, 0) > 0,
                deletable_count=deletable_count,
                deletable_bytes=deletable_bytes,
                deleted_count=deleted_count,
            )
        )
    return views
