# Downsizarr - H264 -> H265/HEVC batch transcoder with web GUI
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DOWNSIZARR_MEDIA_ROOT=/media \
    DOWNSIZARR_DATA_DIR=/config \
    DOWNSIZARR_PORT=8080

# ffmpeg + VAAPI/QSV runtime drivers (Intel iGPU). NVENC requires running on a
# host with NVIDIA drivers and the container started with --gpus all using an
# ffmpeg build that includes nvenc; see README for that path.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        vainfo \
        intel-media-va-driver-non-free \
        mesa-va-drivers \
        libva2 \
        libva-drm2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

VOLUME ["/media", "/config"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.getenv(\"DOWNSIZARR_PORT\",\"8080\")}/healthz')" || exit 1

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${DOWNSIZARR_PORT}"]
