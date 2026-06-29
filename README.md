# Downsizarr

Batch-transcode your H264 video library to **H265 / HEVC** to reclaim disk
space while preserving quality — driven from a clean web GUI, built for Unraid.

Browse your shares, queue a small QA batch first, eyeball the results, then
scale up to bigger batches. Downsizarr **never modifies your originals**: each
converted file is written *next to the source* with a configurable suffix
(`Movie.mkv` → `Movie.hevc.mkv`). It tracks per-file sizes and shows the
running total of space saved over time.

![flow](https://img.shields.io/badge/H264-%E2%86%92%20H265%2FHEVC-3fb950)

## Why

H265/HEVC typically stores the same visual quality in ~40–60% of the space of
H264. Over a large library that's a lot of reclaimed disk — but only if quality
holds up. Downsizarr is built around a **quality-first, QA-first** workflow.

## Features

- **Web GUI** — browse Unraid shares, multi-select files, set a batch limit, go.
- **Quality preserved** — only the *video* stream is re-encoded; audio,
  subtitles, chapters, attachments and metadata are copied byte-for-byte.
- **CRF-based encoding** — default `libx265` CRF 18 / `slow` (visually
  transparent). Fully tunable per batch.
- **Software *and* hardware encoders** — `libx265` (best quality) plus
  `hevc_nvenc` (NVIDIA), `hevc_qsv` (Intel QuickSync), `hevc_vaapi`. Selectable
  per batch in the GUI.
- **Safe by design** — originals are never touched. Output is verified after
  encode (must be HEVC and match the source duration) before it counts as done;
  failed/partial outputs are cleaned up. Existing outputs are skipped.
- **QA-friendly batches** — select a whole folder but cap how many actually run
  with a per-batch limit, so you can test a few before committing.
- **Recursive folder conversion** — point at a top-level folder and queue every
  video underneath it (all subfolders) in one click. Already-HEVC files and
  existing outputs are skipped automatically.
- **VMAF quality scoring** — optionally score each converted file 0–100 against
  its source (95+ ≈ visually transparent) so you can *prove* quality held up,
  not just hope it did.
- **Dedup-ready provenance** — the original file's size is recorded in the
  database and kept forever (even after you delete the original), and is also
  stamped into the converted file's own metadata so it survives without the DB.
- **Metrics** — total space saved, % reduction, per-file deltas, a cumulative
  savings chart, and full conversion history.
- **Live queue** — progress bars, status, and cancel, updated in real time.
- **Post-conversion cleanup** — once a batch finishes, optionally delete the
  original source files (one at a time, or the whole batch) from the **Batches**
  page. Deletion is only ever offered for files whose conversion *completed and
  was verified*, and the HEVC output is never touched.

## Quick start

### Docker Compose

```bash
git clone https://github.com/crescentfreshhh/downsizarr.git
cd downsizarr
# edit docker-compose.yml: point the /media volume at your share
docker compose up -d --build
# open http://<host>:8080
```

### Unraid

1. Add the template (`unraid-template.xml`) or install from Community Apps once
   published.
2. Set **Media Share** to your library (e.g. `/mnt/user/media`) — read/write is
   required because output is written next to each source file.
3. Set **Config / DB** to `/mnt/user/appdata/downsizarr` to persist metrics.
4. Browse to the WebUI and start a small batch.

## How to use it

1. **Browse & Convert** → navigate to a folder, tick a few files.
2. (Optional) **Probe selected** to confirm codec / resolution / size before
   committing.
3. Pick an **encoder**, **CRF** and **preset** (defaults are good for QA).
4. Set a **batch limit** (e.g. `3`) to convert just a few first.
5. **Queue conversion** → watch progress on the **Queue** page.
6. Verify the new `*.hevc.*` files look right, then come back for bigger batches.
7. Track savings on the **Dashboard** / **History**.

### Deleting originals after conversion

When you're happy with a batch's converted files, head to the **Batches** page:

- **Delete one source** — each completed file has a *Delete source* button.
- **Delete a whole batch's sources** — once every job in the batch has finished,
  a single button removes all the originals for that batch and shows how much
  space it frees.

Safety rails: Downsizarr will only delete a source file whose conversion
**completed and was verified**, and only while the converted HEVC output still
exists on disk. Failed, skipped or still-running items are never deleted, and
the converted outputs are never deleted. Deletion is permanent — Downsizarr does
not use a trash/recycle folder — so confirm prompts are shown before any delete.

## Configuration

All settings are environment variables (see [`.env.example`](.env.example)):

| Variable | Default | Description |
|---|---|---|
| `DOWNSIZARR_MEDIA_ROOT` | `/media` | Root dir Downsizarr may browse/write. Everything is sandboxed inside this. |
| `DOWNSIZARR_DATA_DIR` | `/config` | Where the SQLite metrics DB lives. |
| `DOWNSIZARR_OUTPUT_SUFFIX` | `.hevc` | Suffix before the extension on outputs. |
| `DOWNSIZARR_OUTPUT_CONTAINER` | *(empty)* | Force an output container, e.g. `mkv`. Empty = keep source. |
| `DOWNSIZARR_MAX_CONCURRENT` | `1` | Simultaneous transcodes. Keep at 1 for QA / `libx265 slow`. |
| `DOWNSIZARR_DEFAULT_ENCODER` | `libx265` | `libx265`, `hevc_nvenc`, `hevc_qsv`, `hevc_vaapi`. |
| `DOWNSIZARR_DEFAULT_CRF` | `18` | Quality target (lower = better/larger). |
| `DOWNSIZARR_DEFAULT_PRESET` | `slow` | Encoder preset. |
| `DOWNSIZARR_DEFAULT_VMAF` | `false` | Measure VMAF quality by default (slower). |
| `DOWNSIZARR_VMAF_THREADS` | `0` | Threads for VMAF scoring (0 = auto). |
| `DOWNSIZARR_SKIP_ALREADY_HEVC` | `true` | Skip files already in HEVC/H265 instead of re-encoding. |
| `DOWNSIZARR_DURATION_TOLERANCE` | `1.0` | Max allowed source/output duration drift (s) during verification. |
| `DOWNSIZARR_VIDEO_EXTENSIONS` | common set | Extensions treated as convertible video. |

### VMAF quality scoring

Tick **Measure quality (VMAF)** on a batch and each converted file gets a score
from 0–100 comparing it to the original:

- **95–100** — visually indistinguishable from the source.
- **~93+** — generally considered "transparent".
- lower — visible quality loss; worth investigating.

Scores appear on the **Batches** and **History** pages. VMAF runs a second
analysis pass, so batches with it enabled take longer. It requires an ffmpeg
build with `libvmaf` — the bundled Docker image includes one. If `libvmaf`
isn't present, conversions still run normally and the score simply shows "–".

### Deduplication / provenance

Downsizarr records each conversion's **original source file size** and keeps it
permanently — it is *not* erased when you delete the original. It's also written
into the converted file's container as metadata tags
(`DOWNSIZARR_SOURCE_BYTES`, `DOWNSIZARR_SOURCE_NAME`), readable with
`ffprobe -show_entries format_tags`, so the provenance travels with the file
even if the database is lost. This makes later dedup/accounting work possible
long after the originals are gone.

### Quality guidance

- **CRF 16–18** — visually transparent / indistinguishable from source for most
  content. Start here.
- **CRF 19–21** — still very good, noticeably smaller. Good once you trust it.
- **Preset** — `slow`/`slower` give better compression for the same quality at
  the cost of time. For QA on a CPU, `slow` is a sensible balance.
- **10-bit** — optional toggle; reduces banding, slightly larger files.

## Hardware encoding

Software `libx265` gives the best quality-per-byte and works everywhere with no
special setup. Hardware encoders are much faster but trade some efficiency.

- **Intel QSV / VAAPI** — pass the iGPU through: add `/dev/dri:/dev/dri`
  (uncomment the `devices:` block in `docker-compose.yml`, or add the device in
  the Unraid template). The bundled image already includes the Intel VA drivers.
- **NVIDIA NVENC** — requires the host to have NVIDIA drivers +
  `nvidia-container-toolkit`, the container started with GPU access, and an
  `ffmpeg` build with `hevc_nvenc`. The default Debian `ffmpeg` in the image
  does not include NVENC; use an NVIDIA-enabled base/ffmpeg for that path.

## How conversion works

For each file Downsizarr runs roughly:

```
ffmpeg -i INPUT \
  -map 0 -c copy \           # keep ALL streams, copy them verbatim
  -c:v libx265 -preset slow -crf 18 \   # ...except re-encode video to HEVC
  -map_metadata 0 -map_chapters 0 -tag:v hvc1 \
  OUTPUT.hevc.mkv
```

After encoding, the output is probed: it must be HEVC and its duration must
match the source within tolerance, or it's discarded and the job fails. Nothing
is ever written over your source files.

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt pytest
DOWNSIZARR_MEDIA_ROOT=/tmp/media DOWNSIZARR_DATA_DIR=/tmp/cfg \
  uvicorn app.main:app --reload --port 8080
pytest -q
```

### Project layout

```
app/
  main.py         FastAPI routes + templating + lifecycle
  config.py       env-driven settings
  models.py       SQLModel tables + enums (Job, Batch)
  database.py     SQLite engine / sessions (WAL)
  media.py        sandboxed browsing + ffprobe
  transcoder.py   ffmpeg command building + run + verify
  worker.py       background job scheduler + cancellation
  metrics.py      savings aggregation
  templates/      Jinja2 + HTMX views
  static/         CSS + small JS
```

## License

MIT
