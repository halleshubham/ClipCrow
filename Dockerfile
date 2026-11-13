# syntax=docker/dockerfile:1.7
# ClipCrow: web service + renderer in one image. One image runs on CPU or on an NVIDIA GPU (the CUDA 12 libraries come
# from pip wheels; build with --build-arg WITH_CUDA=0 for the smaller CPU-only variant).
#
#   docker build -t clipcrow .                     # CUDA variant
#   docker build --build-arg WITH_CUDA=0 -t clipcrow:cpu .
#   docker run --rm --shm-size=1g clipcrow clipcrow selftest      # can it render?
#
# The image is built and pushed to ghcr.io by .github/workflows/image.yml; docker-compose.yml pulls it.
# It contains GSAP (fetched from npm at build time, GreenSock licence) and a GPL FFmpeg build: keep the package PRIVATE.

ARG PYTHON_VERSION=3.12
FROM node:22-bookworm-slim AS node

FROM python:${PYTHON_VERSION}-slim-bookworm
ARG WITH_CUDA=1
ARG HYPERFRAMES_VERSION=0.9.29
ENV DEBIAN_FRONTEND=noninteractive PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

# Node 22 from the official image: no extra apt repository to trust or to be unreachable.
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

# The libraries headless Chrome needs, plus tini (signal forwarding), curl and xz (the installers, the health check).
RUN APT="-o Acquire::Retries=5 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30" \
 && apt-get $APT update \
 && apt-get $APT install -y --no-install-recommends \
      ca-certificates curl xz-utils tini util-linux \
      libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
      libxfixes3 libxrandr2 libgbm1 libasound2 libpango-1.0-0 libcairo2 libxshmfence1 libxext6 libx11-6 libxcb1 \
      libdbus-1-3 fonts-liberation \
 && rm -rf /var/lib/apt/lists/*

# HyperFrames at the pinned version (it changes pixels between releases) and the Chrome it pins, baked in under /opt so the
# unprivileged user can read it and nothing downloads at run time. The matte model (~170 MB) is fetched on first use into /models.
RUN npm install -g "hyperframes@${HYPERFRAMES_VERSION}" && npm cache clean --force
RUN mkdir -p /opt/hf-home \
 && HOME=/opt/hf-home HYPERFRAMES_NO_TELEMETRY=1 hyperframes browser ensure \
 && HOME=/opt/hf-home hyperframes browser path | tail -1 > /opt/hf-home/browser-path \
 && test -x "$(cat /opt/hf-home/browser-path)" \
 && chmod -R a+rX /opt/hf-home

# The same installers CI uses, so the image runs what the tests run: FFmpeg, the Noto faces, GSAP.
COPY scripts/install_ffmpeg.sh scripts/install_fonts.sh scripts/fetch_gsap.sh /tmp/scripts/
RUN /tmp/scripts/install_ffmpeg.sh /usr/local/bin \
 && /tmp/scripts/install_fonts.sh /opt/fonts \
 && /tmp/scripts/fetch_gsap.sh /opt/clipcrow/vendor \
 && chmod -R a+rX /opt/fonts /opt/clipcrow && rm -rf /tmp/scripts

# Python dependencies first (their own cached layer: they change rarely), then the code on top.
WORKDIR /app
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
COPY pyproject.toml README.md LICENSE NOTICE ./
RUN mkdir clipcrow && touch clipcrow/__init__.py \
 && pip install ".[llm,vision,service,storage,asr,assets]" \
 && if [ "$WITH_CUDA" = "1" ]; then pip install "nvidia-cublas-cu12" "nvidia-cudnn-cu12==9.*"; fi \
 && pip uninstall -y clipcrow
COPY clipcrow ./clipcrow
RUN pip install --no-deps . \
 && python -c "from clipcrow.render import HYPERFRAMES_VERSION as v; import sys; sys.exit(0 if v == '${HYPERFRAMES_VERSION}' else 'the app pins HyperFrames '+v+' but the image installs ${HYPERFRAMES_VERSION}')" \
 && rm -rf /app/clipcrow /app/build

RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin clipcrow \
 && mkdir -p /data /models && chown 10001:10001 /data /models
COPY docker/entrypoint.sh /entrypoint.sh

ENV CLIPCROW_HF_BIN=hyperframes CLIPCROW_FONT_DIR=/opt/fonts CLIPCROW_GSAP=/opt/clipcrow/vendor/gsap.min.js \
    HYPERFRAMES_NO_TELEMETRY=1 HYPERFRAMES_NO_UPDATE_CHECK=1 PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 \
    DRAIN_TIMEOUT_SECONDS=45
VOLUME ["/data", "/models"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/api/config >/dev/null || exit 1

# tini is PID 1 so SIGTERM from a deploy reaches the app, which then drains (finishes or requeues the running job).
ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
CMD ["clipcrow", "serve", "--host", "0.0.0.0", "--port", "8000", "--data", "/data"]
