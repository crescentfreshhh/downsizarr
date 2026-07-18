"""FastAPI application: routes, templating, and worker lifecycle."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import select

from . import __version__, media, metrics
from .config import settings
from .database import init_db, session_scope
from .models import Batch, Encoder, Job, JobStatus
from .worker import manager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("downsizarr")

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
def _vmaf_class(score) -> str:
    if score is None:
        return ""
    if score >= 95:
        return "good"
    if score >= 90:
        return "ok-q"
    return "warn-q"


templates.env.globals["human_bytes"] = metrics.human_bytes
templates.env.globals["human_duration"] = metrics.human_duration
templates.env.globals["vmaf_class"] = _vmaf_class
templates.env.globals["version"] = __version__
templates.env.globals["encoders"] = list(Encoder)
templates.env.globals["settings"] = settings


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    manager.start()
    log.info("Downsizarr %s ready. Media root: %s", __version__, settings.media_root)
    yield
    manager.stop()


app = FastAPI(title="Downsizarr", version=__version__, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    stats = metrics.collect()
    return templates.TemplateResponse(
        "dashboard.html", {"request": request, "stats": stats}
    )


@app.get("/metrics/partial", response_class=HTMLResponse)
def metrics_partial(request: Request):
    stats = metrics.collect()
    return templates.TemplateResponse(
        "_stats.html", {"request": request, "stats": stats}
    )


# --------------------------------------------------------------------------
# Browse + create batch
# --------------------------------------------------------------------------
@app.get("/browse", response_class=HTMLResponse)
def browse(request: Request, path: str = ""):
    try:
        current, entries = media.list_dir(path)
    except (FileNotFoundError, media.PathOutsideRootError) as exc:
        current, entries = "", []
        log.warning("Browse error for %r: %s", path, exc)
    parent = str(Path(current).parent) if current and current != "." else ""
    if parent == ".":
        parent = ""
    return templates.TemplateResponse(
        "browse.html",
        {
            "request": request,
            "current": current,
            "parent": parent,
            "entries": entries,
            "default_encoder": settings.default_encoder,
            "default_crf": settings.default_crf,
            "default_preset": settings.default_preset,
            "default_vmaf": settings.default_vmaf,
            "default_tag_encoder": settings.tag_encoder,
            "default_tag_quality": settings.tag_quality,
            "default_gpu_decode": settings.default_gpu_decode,
        },
    )


@app.post("/probe", response_class=HTMLResponse)
def probe_files(request: Request, files: list[str] = Form(default=[])):
    """Probe selected files and return a codec/size summary partial."""
    results = []
    for rel in files:
        try:
            entry = media.Entry(Path(rel).name, rel, False)
            entry.size = media.safe_path(rel).stat().st_size
            results.append(media.probe_entry(entry))
        except Exception as exc:  # noqa: BLE001
            log.warning("Probe failed for %s: %s", rel, exc)
    return templates.TemplateResponse(
        "_probe.html", {"request": request, "results": results}
    )


@app.post("/batches")
def create_batch(
    files: list[str] = Form(default=[]),
    folder: str = Form(default=""),
    recursive: bool = Form(default=False),
    encoder: str = Form(default=settings.default_encoder),
    crf: int = Form(default=settings.default_crf),
    preset: str = Form(default=settings.default_preset),
    ten_bit: bool = Form(default=False),
    compute_vmaf: bool = Form(default=False),
    tag_encoder: bool = Form(default=False),
    tag_quality: bool = Form(default=False),
    gpu_decode: bool = Form(default=False),
    limit: int = Form(default=0),
    note: str = Form(default=""),
):
    """Create a batch and enqueue jobs.

    Files come from explicit checkbox selections and/or, when ``recursive`` is
    set, every convertible video under ``folder``. Probing happens later in the
    worker so even huge recursive batches enqueue instantly.
    """
    try:
        encoder_enum = Encoder(encoder)
    except ValueError:
        encoder_enum = Encoder.LIBX265

    # Gather candidates: recursive folder expansion first, then explicit picks.
    candidates: list[str] = []
    if recursive:
        try:
            candidates.extend(media.collect_videos(folder, recursive=True))
        except (FileNotFoundError, media.PathOutsideRootError) as exc:
            log.warning("Recursive collect failed for %r: %s", folder, exc)
    candidates.extend(files)

    # De-duplicate while preserving order, then apply the batch limit.
    seen: set[str] = set()
    ordered = [c for c in candidates if not (c in seen or seen.add(c))]
    selected = ordered[:limit] if limit and limit > 0 else ordered

    if not selected:
        return RedirectResponse(url="/browse", status_code=303)

    with session_scope() as session:
        batch = Batch(
            name=note or f"Batch of {len(selected)}",
            encoder=encoder_enum.value,
            crf=crf,
            preset=preset,
            ten_bit=ten_bit,
            compute_vmaf=compute_vmaf,
            tag_encoder=tag_encoder,
            tag_quality=tag_quality,
            gpu_decode=gpu_decode,
            note=note,
        )
        session.add(batch)
        session.flush()  # assign batch.id

        for rel in selected:
            try:
                src = media.safe_path(rel)
            except media.PathOutsideRootError:
                continue
            if not src.exists():
                continue
            # Fast: record size via stat() only; worker probes codec/duration.
            try:
                size = src.stat().st_size
            except OSError:
                size = 0
            session.add(
                Job(
                    batch_id=batch.id,
                    source_path=str(src),
                    encoder=encoder_enum.value,
                    crf=crf,
                    preset=preset,
                    ten_bit=ten_bit,
                    source_size=size,
                )
            )

    return RedirectResponse(url="/jobs", status_code=303)


# --------------------------------------------------------------------------
# Jobs / queue
# --------------------------------------------------------------------------
@app.get("/jobs", response_class=HTMLResponse)
def jobs_page(request: Request):
    return templates.TemplateResponse("jobs.html", {"request": request})


QUEUE_PREVIEW = 15  # how many queued jobs to render before summarising the rest


@app.get("/jobs/partial", response_class=HTMLResponse)
def jobs_partial(request: Request):
    """Scale-safe queue view: never renders more than a handful of rows even if
    thousands of jobs are queued. Heavy counts are done in SQL."""
    from sqlalchemy import func

    with session_scope() as session:
        running = session.exec(
            select(Job).where(Job.status == JobStatus.RUNNING.value).order_by(Job.id)
        ).all()
        queued_preview = session.exec(
            select(Job)
            .where(Job.status == JobStatus.QUEUED.value)
            .order_by(Job.id)
            .limit(QUEUE_PREVIEW)
        ).all()
        queued_total = int(
            session.execute(
                select(func.count(Job.id)).where(Job.status == JobStatus.QUEUED.value)
            ).scalar() or 0
        )
        recent = session.exec(
            select(Job)
            .where(Job.status.notin_([JobStatus.QUEUED.value, JobStatus.RUNNING.value]))
            .order_by(Job.finished_at.desc())
            .limit(25)
        ).all()
    est = metrics.queue_estimate()
    return templates.TemplateResponse(
        "_queue.html",
        {
            "request": request,
            "running": running,
            "queued_preview": queued_preview,
            "queued_total": queued_total,
            "queued_hidden": max(0, queued_total - len(queued_preview)),
            "recent": recent,
            "est": est,
        },
    )


@app.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: int):
    manager.cancel(job_id)
    return RedirectResponse(url="/jobs", status_code=303)


# --------------------------------------------------------------------------
# Source-file deletion (post-conversion cleanup)
# --------------------------------------------------------------------------
def _delete_source_for_job(session, job: Job) -> tuple[bool, str]:
    """Delete a job's SOURCE file, but only when it's provably safe.

    Requires: the job completed, its source hasn't already been removed, and the
    verified HEVC output still exists on disk and is non-empty.
    """
    if job.status != JobStatus.COMPLETED.value:
        return False, "job not completed"
    if job.source_deleted:
        return False, "already deleted"

    out = Path(job.output_path) if job.output_path else None
    if not out or not out.exists() or out.stat().st_size == 0:
        return False, "converted output missing — refusing to delete source"

    try:
        media.remove_within_root(job.source_path)
    except FileNotFoundError:
        # Source is already gone; reconcile our bookkeeping.
        job.source_deleted = True
        session.add(job)
        return True, "source already gone"
    except (media.PathOutsideRootError, IsADirectoryError) as exc:
        return False, str(exc)

    job.source_deleted = True
    session.add(job)
    log.info("Deleted source for job %s: %s", job.id, job.source_path)
    return True, "deleted"


@app.post("/jobs/{job_id}/delete-source")
def delete_job_source(job_id: int):
    with session_scope() as session:
        job = session.get(Job, job_id)
        if job:
            _delete_source_for_job(session, job)
    return RedirectResponse(url="/batches", status_code=303)


@app.post("/batches/{batch_id}/delete-sources")
def delete_batch_sources(batch_id: int):
    """Delete the sources for every completed job in a finished batch."""
    with session_scope() as session:
        jobs = session.exec(select(Job).where(Job.batch_id == batch_id)).all()
        active = {JobStatus.QUEUED.value, JobStatus.RUNNING.value}
        if jobs and not any(j.status in active for j in jobs):
            for job in jobs:
                _delete_source_for_job(session, job)
    return RedirectResponse(url="/batches", status_code=303)


@app.get("/batches", response_class=HTMLResponse)
def batches_page(request: Request):
    return templates.TemplateResponse(
        "batches.html", {"request": request, "batches": metrics.batch_summaries()}
    )


@app.get("/history", response_class=HTMLResponse)
def history(request: Request):
    with session_scope() as session:
        jobs = session.exec(
            select(Job)
            .where(Job.status == JobStatus.COMPLETED.value)
            .order_by(Job.finished_at.desc())
        ).all()
        stats = metrics.collect()
    return templates.TemplateResponse(
        "history.html", {"request": request, "jobs": jobs, "stats": stats}
    )


@app.get("/healthz")
def healthz():
    return {"status": "ok", "version": __version__}
