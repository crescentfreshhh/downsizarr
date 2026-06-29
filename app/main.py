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
templates.env.globals["human_bytes"] = metrics.human_bytes
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
    encoder: str = Form(default=settings.default_encoder),
    crf: int = Form(default=settings.default_crf),
    preset: str = Form(default=settings.default_preset),
    ten_bit: bool = Form(default=False),
    limit: int = Form(default=0),
    note: str = Form(default=""),
):
    """Create a batch and enqueue jobs for the selected files."""
    try:
        encoder_enum = Encoder(encoder)
    except ValueError:
        encoder_enum = Encoder.LIBX265

    selected = files[:limit] if limit and limit > 0 else files

    with session_scope() as session:
        batch = Batch(
            name=note or f"Batch of {len(selected)}",
            encoder=encoder_enum.value,
            crf=crf,
            preset=preset,
            ten_bit=ten_bit,
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
            try:
                probed = media.probe(src)
            except Exception:
                probed = media.ProbeResult("", 0.0, 0, 0, src.stat().st_size)

            job = Job(
                batch_id=batch.id,
                source_path=str(src),
                encoder=encoder_enum.value,
                crf=crf,
                preset=preset,
                ten_bit=ten_bit,
                source_codec=probed.codec,
                source_size=probed.size,
                source_duration=probed.duration,
                width=probed.width,
                height=probed.height,
            )
            session.add(job)

    return RedirectResponse(url="/jobs", status_code=303)


# --------------------------------------------------------------------------
# Jobs / queue
# --------------------------------------------------------------------------
@app.get("/jobs", response_class=HTMLResponse)
def jobs_page(request: Request):
    return templates.TemplateResponse("jobs.html", {"request": request})


@app.get("/jobs/partial", response_class=HTMLResponse)
def jobs_partial(request: Request):
    active_states = {JobStatus.QUEUED.value, JobStatus.RUNNING.value}
    with session_scope() as session:
        rows = session.exec(select(Job).order_by(Job.id.desc())).all()
        active = [j for j in rows if j.status in active_states]
        recent = [j for j in rows if j.status not in active_states][:25]
    return templates.TemplateResponse(
        "_queue.html",
        {"request": request, "active": active, "recent": recent},
    )


@app.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: int):
    manager.cancel(job_id)
    return RedirectResponse(url="/jobs", status_code=303)


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
