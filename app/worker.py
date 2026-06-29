"""Background job processor.

A single daemon thread polls the DB for queued jobs and dispatches them to a
thread pool sized to ``settings.max_concurrent``. Each worker thread shells out
to ffmpeg via :mod:`app.transcoder` and writes progress/results back to the DB.

Cancellation works by recording a request, killing the live ffmpeg process, and
letting the worker mark the job ``canceled``.
"""
from __future__ import annotations

import logging
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict

from sqlmodel import select

from .config import settings
from .database import session_scope
from .models import Job, JobStatus, utcnow
from .transcoder import TranscodeError, output_path_for, transcode

log = logging.getLogger("downsizarr.worker")


class JobManager:
    def __init__(self) -> None:
        self._procs: Dict[int, subprocess.Popen] = {}
        self._cancel: set[int] = set()
        self._lock = threading.Lock()
        self._active: set[int] = set()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ----- lifecycle -------------------------------------------------------
    def start(self) -> None:
        self._recover_interrupted()
        self._thread = threading.Thread(
            target=self._loop, name="downsizarr-scheduler", daemon=True
        )
        self._thread.start()
        log.info("Job scheduler started (max_concurrent=%s)", settings.max_concurrent)

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            for proc in self._procs.values():
                try:
                    proc.kill()
                except Exception:
                    pass

    def _recover_interrupted(self) -> None:
        """Requeue jobs left RUNNING by a previous crash/restart."""
        with session_scope() as session:
            rows = session.exec(
                select(Job).where(Job.status == JobStatus.RUNNING.value)
            ).all()
            for job in rows:
                job.status = JobStatus.QUEUED.value
                job.progress = 0.0
                job.started_at = None
                session.add(job)
            if rows:
                log.info("Recovered %d interrupted job(s)", len(rows))

    # ----- scheduling ------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:  # pragma: no cover - keep the loop alive
                log.exception("Scheduler tick failed")
            self._stop.wait(2.0)

    def _tick(self) -> None:
        with self._lock:
            free = settings.max_concurrent - len(self._active)
        if free <= 0:
            return

        with session_scope() as session:
            queued = session.exec(
                select(Job)
                .where(Job.status == JobStatus.QUEUED.value)
                .order_by(Job.id)
                .limit(free)
            ).all()
            claimed: list[int] = []
            for job in queued:
                job.status = JobStatus.RUNNING.value
                job.started_at = utcnow()
                session.add(job)
                claimed.append(job.id)

        for job_id in claimed:
            with self._lock:
                self._active.add(job_id)
            threading.Thread(
                target=self._run_job, args=(job_id,), daemon=True
            ).start()

    # ----- per-job execution ----------------------------------------------
    def _run_job(self, job_id: int) -> None:
        try:
            self._execute(job_id)
        except Exception:  # pragma: no cover
            log.exception("Job %s crashed", job_id)
            self._finish(job_id, JobStatus.FAILED, error="Internal worker error")
        finally:
            with self._lock:
                self._active.discard(job_id)
                self._procs.pop(job_id, None)
                self._cancel.discard(job_id)

    def _execute(self, job_id: int) -> None:
        with session_scope() as session:
            job = session.get(Job, job_id)
            if job is None:
                return
            src = Path(job.source_path)
            encoder = job.encoder
            crf = job.crf
            preset = job.preset
            ten_bit = job.ten_bit
            duration = job.source_duration

        if job_id in self._cancel:
            self._finish(job_id, JobStatus.CANCELED)
            return

        if not src.exists():
            self._finish(job_id, JobStatus.FAILED, error=f"Source missing: {src}")
            return

        dest = output_path_for(src)
        if dest.exists():
            self._finish(
                job_id,
                JobStatus.SKIPPED,
                error=f"Output already exists: {dest.name}",
            )
            return

        last_write = 0.0

        def on_progress(pct: float) -> None:
            nonlocal last_write
            now = time.monotonic()
            if now - last_write >= 1.0 or pct >= 100.0:
                last_write = now
                self._set_progress(job_id, pct)

        def sink(proc: subprocess.Popen) -> None:
            with self._lock:
                self._procs[job_id] = proc
                if job_id in self._cancel:
                    proc.kill()

        from .models import Encoder

        try:
            result = transcode(
                src,
                dest,
                encoder=Encoder(encoder),
                crf=crf,
                preset=preset,
                ten_bit=ten_bit,
                duration=duration,
                on_progress=on_progress,
                process_sink=sink,
            )
        except TranscodeError as exc:
            if job_id in self._cancel or "canceled" in str(exc).lower():
                self._finish(job_id, JobStatus.CANCELED, error="Canceled by user")
            else:
                self._finish(job_id, JobStatus.FAILED, error=str(exc))
            return

        self._finish(
            job_id,
            JobStatus.COMPLETED,
            output_path=str(result.output_path),
            output_size=result.output_size,
            progress=100.0,
        )

    # ----- DB mutation helpers --------------------------------------------
    def _set_progress(self, job_id: int, pct: float) -> None:
        with session_scope() as session:
            job = session.get(Job, job_id)
            if job and job.status == JobStatus.RUNNING.value:
                job.progress = round(pct, 1)
                session.add(job)

    def _finish(self, job_id: int, status: JobStatus, **fields) -> None:
        with session_scope() as session:
            job = session.get(Job, job_id)
            if job is None:
                return
            job.status = status.value
            job.finished_at = utcnow()
            for key, value in fields.items():
                setattr(job, key, value)
            session.add(job)
        log.info("Job %s -> %s", job_id, status.value)

    # ----- public controls -------------------------------------------------
    def cancel(self, job_id: int) -> bool:
        with self._lock:
            self._cancel.add(job_id)
            proc = self._procs.get(job_id)
        if proc:
            try:
                proc.kill()
            except Exception:
                pass
            return True
        # Not running yet: cancel it in place if still queued.
        with session_scope() as session:
            job = session.get(Job, job_id)
            if job and job.status == JobStatus.QUEUED.value:
                job.status = JobStatus.CANCELED.value
                job.finished_at = utcnow()
                session.add(job)
                return True
        return False


manager = JobManager()
