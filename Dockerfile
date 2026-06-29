# Downsizarr - H264 -> H265/HEVC batch transcoder with web GUI
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DOWNSIZARR_MEDIA_ROOT=/media \
    DOWNSIZARR_DATA_DIR=/config \
    DOWNSIZARR_PORT=8080

# Runtime libs + VAAPI/QSV drivers (Intel iGPU). We do NOT use the distro
# ffmpeg because it lacks libvmaf (quality scoring) and nvenc; instead we drop
# in a static BtbN build that bundles libx265, libvmaf, nvenc, VAAPI and QSV.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        xz-utils \
        vainfo \
        intel-media-va-driver-non-free \
        mesa-va-drivers \
        libva2 \
        libva-drm2 \
    && rm -rf /var/lib/apt/lists/*

# Static ffmpeg/ffprobe with libx265 + libvmaf + nvenc (GPL build).
# Override FFMPEG_BUILD_URL at build time to pin a specific release if desired.
ARG FFMPEG_BUILD_URL=https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz
RUN set -eux; \
    curl -fsSL "$FFMPEG_BUILD_URL" -o /tmp/ffmpeg.tar.xz; \
    mkdir -p /tmp/ffmpeg; \
    tar -xf /tmp/ffmpeg.tar.xz -C /tmp/ffmpeg --strip-components=1; \
    cp /tmp/ffmpeg/bin/ffmpeg /tmp/ffmpeg/bin/ffprobe /usr/local/bin/; \
    chmod +x /usr/local/bin/ffmpeg /usr/local/bin/ffprobe; \
    rm -rf /tmp/ffmpeg /tmp/ffmpeg.tar.xz; \
    ffmpeg -hide_banner -filters | grep -q ' libvmaf '  # fail build if missing

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

VOLUME ["/media", "/config"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.getenv(\"DOWNSIZARR_PORT\",\"8080\")}/healthz')" || exit 1

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${DOWNSIZARR_PORT}"]
