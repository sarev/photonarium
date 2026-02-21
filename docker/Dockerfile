# =============================================================================
# Photonarium Docker Image
#
# Build variants:
#   CPU-only:  docker build -t photonarium:latest -f docker/Dockerfile .
#   CUDA:      docker build --build-arg TORCH_INDEX=.../cu126 -t photonarium:cu126 ...
#   Intel:     docker build --build-arg INSTALL_IPEX=1 -t photonarium:intel ...
#
# Or use the Makefile: make build, make build-cu126, make build-intel, etc.
#
# Prerequisites:
#   Models must be pre-downloaded to docker/models/ before building:
#   HF_HOME=docker/models/huggingface TORCH_HOME=docker/models/torch \
#   python download_models.py --standalone --data-dir docker/models
# =============================================================================

FROM python:3.12-slim AS base

# Build arguments
ARG TORCH_INDEX=https://download.pytorch.org/whl/cpu
ARG INSTALL_IPEX=0

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

# -----------------------------------------------------------------------------
# Layer 1: ML Models (rarely changes, ~2.5GB)
# -----------------------------------------------------------------------------
# Models are pre-downloaded locally via download_models.py --standalone.
# This layer is independent of pip installs and app code, so it's cached
# even when dependencies or code change.

# LAION aesthetic head and NIMA weights
COPY docker/models/.laion-aesthetic-head.pth /defaults/
COPY docker/models/.nima-mobilenetv2-ava.pth /defaults/

# HuggingFace models (OpenCLIP, BLIP)
COPY docker/models/huggingface/ /root/.cache/huggingface/

# PyTorch models (FaceNet InceptionResnetV1)
COPY docker/models/torch/ /root/.cache/torch/

# -----------------------------------------------------------------------------
# Layer 2: PyTorch (changes per variant, ~1-4GB)
# -----------------------------------------------------------------------------
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

# -----------------------------------------------------------------------------
# Layer 3: Python dependencies (rarely changes)
# -----------------------------------------------------------------------------
COPY docker/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install facenet-pytorch with --no-deps to avoid version conflicts
RUN pip install --no-cache-dir --no-deps facenet-pytorch

# -----------------------------------------------------------------------------
# Layer 4: Application code (changes frequently, small)
# -----------------------------------------------------------------------------
COPY app/ ./app/
COPY download_models.py .

# Copy entrypoint script
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# -----------------------------------------------------------------------------
# Metadata and runtime config
# -----------------------------------------------------------------------------
# OCI image labels (see: https://github.com/opencontainers/image-spec/blob/main/annotations.md)
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

EXPOSE 5000
VOLUME ["/config", "/catalogue"]

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 \
    CMD curl -sf http://localhost:5000/api/health || exit 1

ENTRYPOINT ["/entrypoint.sh"]
