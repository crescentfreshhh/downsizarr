# Downsizarr - H264 -> H265/HEVC batch transcoder with web GUI
FROM python:3.12-slim

LABEL org.opencontainers.image.source="https://github.com/crescentfreshhh/downsizarr" \
      org.opencontainers.image.description="Batch H264 to H265/HEVC transcoder with a web GUI (GPU/CPU)" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DOWNSIZARR_MEDIA_ROOT=/media \
    DOWNSIZARR_DATA_DIR=/config \
    DOWNSIZARR_PORT=8080

# Essential runtime deps (all in Debian main). We do NOT use the distro ffmpeg
# because it lacks libvmaf (quality scoring) and nvenc; instead we drop in a
# static BtbN build (next step) that bundles libx265, libvmaf, nvenc, etc.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        xz-utils \
        vainfo \
        mesa-va-drivers \
        libva2 \
        libva-drm2 \
    && rm -rf /var/lib/apt/lists/*

# Optional Intel QSV/VAAPI hardware driver (lives in Debian non-free). Best
# effort: NVIDIA and CPU encoding don't need it, so a failure here must NOT
# break the image build.
RUN set -eux; \
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i 's/^Components: main$/Components: main contrib non-free non-free-firmware/' \
            /etc/apt/sources.list.d/debian.sources; \
    fi; \
    apt-get update \
    && apt-get install -y --no-install-recommends intel-media-va-driver-non-free \
    && rm -rf /var/lib/apt/lists/* \
    || echo "WARN: intel-media-va-driver-non-free unavailable; Intel QSV may be limited";

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
