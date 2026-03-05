# =============================================================================
# Photonarium Docker Image (Variant)
#
# Extends the base image with variant-specific PyTorch and ML packages.
#
# Build variants:
#   CPU:   docker build --build-arg BASE_IMAGE=photonarium-base:x64 -t photonarium:cpu ...
#   CUDA:  docker build --build-arg BASE_IMAGE=photonarium-base:x64 --build-arg TORCH_INDEX=.../cu126 ...
#   ARM64: docker buildx build --build-arg BASE_IMAGE=photonarium-base:arm64 ...
#
# Or use the Makefile: make build, make build-cu126, make build-intel, etc.
#
# Prerequisites:
#   The base image must be built first: make base-x64 (or make base-arm64)
# =============================================================================

# syntax=docker/dockerfile:1

ARG BASE_IMAGE=photonarium-base:x64
FROM ${BASE_IMAGE}

# Build arguments that affect package installation layers.
# Version/metadata ARGs are declared later (after pip installs) so that
# changing them doesn't invalidate the expensive dependency layers.
ARG TORCH_INDEX=https://download.pytorch.org/whl/cpu
ARG INSTALL_IPEX=0

# -----------------------------------------------------------------------------
# PyTorch (variant-specific, ~1-4GB)
# -----------------------------------------------------------------------------
# TORCH_INDEX can be empty for ARM64 builds (use default PyPI)
# --mount=type=cache keeps downloaded wheels across builds so a layer cache
# miss doesn't require re-downloading multi-GB packages over the network.
RUN --mount=type=cache,target=/root/.cache/pip \
    if [ -n "${TORCH_INDEX}" ]; then \
        pip install --retries 3 torch torchvision torchaudio --index-url ${TORCH_INDEX}; \
    else \
        pip install --retries 3 torch torchvision torchaudio; \
    fi

# Install Intel Extension for PyTorch if requested
RUN --mount=type=cache,target=/root/.cache/pip \
    if [ "${INSTALL_IPEX}" = "1" ]; then \
        pip install --retries 3 intel-extension-for-pytorch; \
    fi

# -----------------------------------------------------------------------------
# ML packages that depend on torch
# -----------------------------------------------------------------------------
COPY docker/requirements-ml.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --retries 3 -r requirements-ml.txt

# Install facenet-pytorch with --no-deps to avoid version conflicts
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --retries 3 --no-deps facenet-pytorch

# -----------------------------------------------------------------------------
# Application code (changes frequently, ~3MB)
# -----------------------------------------------------------------------------
COPY app/ ./app/
COPY download_models.py .

# Copy entrypoint script
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# -----------------------------------------------------------------------------
# Metadata and runtime config
# -----------------------------------------------------------------------------
# Version ARGs declared here — after all pip installs — so that version bumps
# only invalidate the cheap final layers, not the multi-GB dependency layers.
ARG VERSION=dev
ARG GIT_COMMIT=unknown
ARG BUILD_DATE=unknown
ARG VARIANT=cpu

# Write version info to a file so the app can read it (git isn't available in container)
RUN echo "${VERSION}" > /app/VERSION && \
    echo "${GIT_COMMIT}" >> /app/VERSION && \
    echo "${BUILD_DATE}" >> /app/VERSION

# OCI image labels (see: https://github.com/opencontainers/image-spec/blob/main/annotations.md)
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
