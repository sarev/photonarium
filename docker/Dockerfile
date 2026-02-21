# =============================================================================
# Photonarium Docker Image
#
# Build variants:
#   CPU-only:  docker build -t photonarium:latest -f docker/Dockerfile .
#   CUDA:      docker build --build-arg TORCH_INDEX=.../cu126 -t photonarium:cu126 ...
#   Intel:     docker build --build-arg INSTALL_IPEX=1 -t photonarium:intel ...
#
# Or use the Makefile: make build, make build-cu126, make build-intel, etc.
# =============================================================================

FROM python:3.12-slim AS base

# Build arguments
ARG TORCH_INDEX=https://download.pytorch.org/whl/cpu
ARG INSTALL_IPEX=0
ARG HF_TOKEN=""

# Version metadata (passed from Makefile)
ARG VERSION=dev
ARG GIT_COMMIT=unknown
ARG BUILD_DATE=unknown
ARG VARIANT=cpu

# System dependencies for opencv-headless, Pillow, rawpy, and gosu
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    libraw-dev \
    gcc \
    g++ \
    curl \
    gosu \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install PyTorch first (largest dependency, good for layer caching)
# TORCH_INDEX can be empty for ARM64 builds (use default PyPI)
RUN if [ -n "${TORCH_INDEX}" ]; then \
        pip install --no-cache-dir torch torchvision torchaudio --index-url ${TORCH_INDEX}; \
    else \
        pip install --no-cache-dir torch torchvision torchaudio; \
    fi

# Install Intel Extension for PyTorch if requested
RUN if [ "${INSTALL_IPEX}" = "1" ]; then \
        pip install --no-cache-dir intel-extension-for-pytorch; \
    fi

# Copy and install Python dependencies
COPY docker/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install facenet-pytorch with --no-deps to avoid version conflicts
RUN pip install --no-cache-dir --no-deps facenet-pytorch

# Copy application source
COPY app/ ./app/
COPY download_models.py .

# Download all ML models into a staging directory.
# On first run, entrypoint.sh copies them to /config (the user's volume).
# This includes: OpenCLIP, BLIP, LAION aesthetic head, NIMA weights.
# HF_TOKEN is optional but speeds up downloads and avoids rate limits.
# Safety: remove any token files that HF might create (in same layer to prevent leaks).
ARG HF_TOKEN
RUN mkdir -p /defaults && \
    HF_HUB_OFFLINE=0 HF_TOKEN="${HF_TOKEN}" python download_models.py --data-dir /defaults && \
    rm -f /root/.cache/huggingface/token /defaults/.cache/huggingface/token 2>/dev/null || true

# Copy entrypoint script
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# OCI image labels (see: https://github.com/opencontainers/image-spec/blob/main/annotations.md)
# Placed late in Dockerfile to avoid cache invalidation on version bumps.
ARG VERSION
ARG GIT_COMMIT
ARG BUILD_DATE
ARG VARIANT
LABEL org.opencontainers.image.title="Photonarium" \
      org.opencontainers.image.description="Local AI-powered photo catalogue with semantic search" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${GIT_COMMIT}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.source="https://github.com/7thsw/photonarium" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.vendor="7thsw" \
      io.photonarium.variant="${VARIANT}"

# Runtime configuration
EXPOSE 5000
VOLUME ["/config", "/catalogue"]

# Health check (requires /api/health endpoint - Phase 1 backend change)
HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 \
    CMD curl -sf http://localhost:5000/api/health || exit 1

ENTRYPOINT ["/entrypoint.sh"]
