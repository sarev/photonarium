# =============================================================================
# Photonarium Docker Build
#
# Usage:
#   make              Show available targets (help)
#   make models       Download ML models (run once, or after config.py changes)
#   make base-x64     Build shared base image for x86_64 variants
#   make build-cpu    Build CPU-only image (default for most NAS devices)
#   make build-cu126  Build CUDA 12.6 image (RTX 30xx, 40xx)
#   make all-images   Build all variants
#   make test         Run container smoke test
#
# Requires: GNU Make, Docker, docker-compose, Python venv with dependencies
# =============================================================================

# BuildKit is required for --mount=type=cache (pip download caching)
export DOCKER_BUILDKIT := 1

IMAGE_NAME := photonarium
BASE_IMAGE := photonarium-base
VERSION    := $(shell git describe --tags --always 2>/dev/null || echo dev)
GIT_COMMIT := $(shell git rev-parse --short HEAD 2>/dev/null || echo unknown)
BUILD_DATE := $(shell date -u +%Y-%m-%dT%H:%M:%SZ)

# PyTorch wheel index URLs
TORCH_CPU   := https://download.pytorch.org/whl/cpu
TORCH_CU118 := https://download.pytorch.org/whl/cu118
TORCH_CU126 := https://download.pytorch.org/whl/cu126
TORCH_CU128 := https://download.pytorch.org/whl/cu128

# Number of recent release versions to keep (override: make prune-all KEEP=3)
KEEP ?= 2

# Compose file location
COMPOSE_FILE := docker/docker-compose.yml
PHOTOS_OVERLAY := docker/docker-compose.photos.yml

# Tutorial examples for testing
TEST_PHOTOS := $(CURDIR)/tools/mktutorial/examples

# Model cache directory
MODELS_DIR := docker/models

# HuggingFace token (reads from HF CLI cache if not already set)
HF_TOKEN ?= $(shell cat ~/.cache/huggingface/token 2>/dev/null)

# Marker directory for dependency tracking
MARKER_DIR := .build

# Colours for help output (disabled if not a terminal)
CYAN  := $(shell tput setaf 6 2>/dev/null || echo "")
RESET := $(shell tput sgr0 2>/dev/null || echo "")

# DockerHub repositories
DOCKER_REPO := 7thsw/photonarium
BASE_REPO   := 7thsw/photonarium-base

# Variant tags we publish
VARIANT_TAGS := latest cpu cu118 cu126 cu128 intel arm64

# =============================================================================
# Marker files for dependency tracking
# =============================================================================

MODELS_MARKER     := $(MARKER_DIR)/models
BASE_X64_MARKER   := $(MARKER_DIR)/base-x64
BASE_ARM64_MARKER := $(MARKER_DIR)/base-arm64
CPU_MARKER        := $(MARKER_DIR)/cpu
CU118_MARKER      := $(MARKER_DIR)/cu118
CU126_MARKER      := $(MARKER_DIR)/cu126
CU128_MARKER      := $(MARKER_DIR)/cu128
INTEL_MARKER      := $(MARKER_DIR)/intel
ARM64_MARKER      := $(MARKER_DIR)/arm64

# Common dependencies for all variant builds
VARIANT_DEPS := docker/Dockerfile docker/requirements-ml.txt docker/entrypoint.sh \
                $(wildcard app/*.py) $(wildcard app/static/*.js) $(wildcard app/static/*.css)

.PHONY: models base-x64 base-arm64 build build-cpu build-cu118 build-cu126 build-cu128 build-intel build-arm64 \
        all-x64 all-images use test test-photos up up-photos down logs shell \
        push-base push-latest push-cu118 push-cu126 push-cu128 push-intel push-arm64 push \
        prune prune-all clean clean-all help

# =============================================================================
# Model download (depends on config.py)
# =============================================================================

$(MODELS_MARKER): app/config.py download_models.py
	@echo "Downloading ML models to $(MODELS_DIR)/..."
	@mkdir -p $(MODELS_DIR)
	HF_TOKEN=$(HF_TOKEN) \
	HF_HOME=$(MODELS_DIR)/huggingface \
	TORCH_HOME=$(MODELS_DIR)/torch \
	python3 download_models.py --data-dir $(MODELS_DIR)
	@mkdir -p $(MARKER_DIR) && touch $@
	@echo ""
	@echo "Models downloaded. You can now run 'make base-x64' or 'make build'."

models: $(MODELS_MARKER)  ## Download ML models (re-runs if config.py changes)

# =============================================================================
# Base images
# =============================================================================

$(BASE_X64_MARKER): $(MODELS_MARKER) docker/Dockerfile.base docker/requirements-base.txt
	@echo "Building base image (x64)..."
	docker build \
		-f docker/Dockerfile.base \
		-t $(BASE_IMAGE):x64 \
		-t $(BASE_IMAGE):latest \
		.
	@mkdir -p $(MARKER_DIR) && touch $@

$(BASE_ARM64_MARKER): $(MODELS_MARKER) docker/Dockerfile.base docker/requirements-base.txt
	@echo "Building base image (arm64)..."
	docker buildx build \
		--platform linux/arm64 \
		--load \
		-f docker/Dockerfile.base \
		-t $(BASE_IMAGE):arm64 \
		.
	@mkdir -p $(MARKER_DIR) && touch $@

base-x64: $(BASE_X64_MARKER)      ## Build x64 base image
base-arm64: $(BASE_ARM64_MARKER)  ## Build arm64 base image

# =============================================================================
# Variant images (depend on base + app code)
# =============================================================================

$(CPU_MARKER): $(VARIANT_DEPS) | $(BASE_X64_MARKER)
	docker build \
		--build-arg BASE_IMAGE=$(BASE_IMAGE):x64 \
		--build-arg TORCH_INDEX=$(TORCH_CPU) \
		--build-arg VERSION=$(VERSION) \
		--build-arg GIT_COMMIT=$(GIT_COMMIT) \
		--build-arg BUILD_DATE=$(BUILD_DATE) \
		--build-arg VARIANT=cpu \
		-t $(IMAGE_NAME):latest \
		-t $(IMAGE_NAME):cpu \
		-t $(IMAGE_NAME):$(VERSION)-cpu \
		-f docker/Dockerfile .
	@mkdir -p $(MARKER_DIR) && touch $@

$(CU118_MARKER): $(VARIANT_DEPS) | $(BASE_X64_MARKER)
	docker build \
		--build-arg BASE_IMAGE=$(BASE_IMAGE):x64 \
		--build-arg TORCH_INDEX=$(TORCH_CU118) \
		--build-arg VERSION=$(VERSION) \
		--build-arg GIT_COMMIT=$(GIT_COMMIT) \
		--build-arg BUILD_DATE=$(BUILD_DATE) \
		--build-arg VARIANT=cu118 \
		-t $(IMAGE_NAME):cu118 \
		-t $(IMAGE_NAME):$(VERSION)-cu118 \
		-f docker/Dockerfile .
	@mkdir -p $(MARKER_DIR) && touch $@

$(CU126_MARKER): $(VARIANT_DEPS) | $(BASE_X64_MARKER)
	docker build \
		--build-arg BASE_IMAGE=$(BASE_IMAGE):x64 \
		--build-arg TORCH_INDEX=$(TORCH_CU126) \
		--build-arg VERSION=$(VERSION) \
		--build-arg GIT_COMMIT=$(GIT_COMMIT) \
		--build-arg BUILD_DATE=$(BUILD_DATE) \
		--build-arg VARIANT=cu126 \
		-t $(IMAGE_NAME):cu126 \
		-t $(IMAGE_NAME):$(VERSION)-cu126 \
		-f docker/Dockerfile .
	@mkdir -p $(MARKER_DIR) && touch $@

$(CU128_MARKER): $(VARIANT_DEPS) | $(BASE_X64_MARKER)
	docker build \
		--build-arg BASE_IMAGE=$(BASE_IMAGE):x64 \
		--build-arg TORCH_INDEX=$(TORCH_CU128) \
		--build-arg VERSION=$(VERSION) \
		--build-arg GIT_COMMIT=$(GIT_COMMIT) \
		--build-arg BUILD_DATE=$(BUILD_DATE) \
		--build-arg VARIANT=cu128 \
		-t $(IMAGE_NAME):cu128 \
		-t $(IMAGE_NAME):$(VERSION)-cu128 \
		-f docker/Dockerfile .
	@mkdir -p $(MARKER_DIR) && touch $@

$(INTEL_MARKER): $(VARIANT_DEPS) | $(BASE_X64_MARKER)
	docker build \
		--build-arg BASE_IMAGE=$(BASE_IMAGE):x64 \
		--build-arg TORCH_INDEX=$(TORCH_CPU) \
		--build-arg INSTALL_IPEX=1 \
		--build-arg VERSION=$(VERSION) \
		--build-arg GIT_COMMIT=$(GIT_COMMIT) \
		--build-arg BUILD_DATE=$(BUILD_DATE) \
		--build-arg VARIANT=intel \
		-t $(IMAGE_NAME):intel \
		-t $(IMAGE_NAME):$(VERSION)-intel \
		-f docker/Dockerfile .
	@mkdir -p $(MARKER_DIR) && touch $@

$(ARM64_MARKER): $(VARIANT_DEPS) | $(BASE_ARM64_MARKER)
	docker buildx build \
		--platform linux/arm64 \
		--load \
		--build-arg BASE_IMAGE=$(BASE_IMAGE):arm64 \
		--build-arg TORCH_INDEX= \
		--build-arg VERSION=$(VERSION) \
		--build-arg GIT_COMMIT=$(GIT_COMMIT) \
		--build-arg BUILD_DATE=$(BUILD_DATE) \
		--build-arg VARIANT=arm64 \
		-t $(IMAGE_NAME):arm64 \
		-t $(IMAGE_NAME):$(VERSION)-arm64 \
		-f docker/Dockerfile .
	@mkdir -p $(MARKER_DIR) && touch $@

build-cpu: $(CPU_MARKER)      ## Build CPU-only image (default, ~4.5 GB)
build-cu118: $(CU118_MARKER)  ## Build CUDA 11.8 image (GTX 10xx, RTX 20xx)
build-cu126: $(CU126_MARKER)  ## Build CUDA 12.6 image (RTX 30xx, 40xx)
build-cu128: $(CU128_MARKER)  ## Build CUDA 12.8 image (RTX 50xx / Blackwell)
build-intel: $(INTEL_MARKER)  ## Build Intel iGPU image (Celeron/Atom NAS)
build-arm64: $(ARM64_MARKER)  ## Build ARM64 image (Raspberry Pi, Apple Silicon)
build: build-cpu              ## Alias for build-cpu

all-x64: $(CPU_MARKER) $(CU118_MARKER) $(CU126_MARKER) $(CU128_MARKER) $(INTEL_MARKER)  ## Build all x64 variants
all-images: all-x64 $(ARM64_MARKER)  ## Build all image variants

# =============================================================================
# Runtime targets
# =============================================================================

# Dynamically discover which photonarium images are built
BUILT_TAGS = $(shell docker images --format '{{.Tag}}' $(IMAGE_NAME) 2>/dev/null | grep -v '<none>' | sort -u | tr '\n' ' ')

use:  ## Select which image variant to run (e.g., make use TAG=cu126)
ifndef TAG
	@echo "Usage: make use TAG=<variant>"
	@echo ""
	@built_tags="$(BUILT_TAGS)"; \
	if [ -z "$$built_tags" ]; then \
		echo "$(CYAN)No images built yet.$(RESET)"; \
		echo ""; \
		echo "Build an image first:"; \
		echo "  make build         CPU-only (default)"; \
		echo "  make build-cu126   CUDA 12.6 (RTX 30xx/40xx)"; \
		echo "  make build-cu118   CUDA 11.8 (older GPUs)"; \
		echo "  make build-cu128   CUDA 12.8 (RTX 50xx)"; \
		echo "  make build-intel   Intel iGPU"; \
		echo "  make build-arm64   ARM64 (Raspberry Pi, Apple Silicon)"; \
	else \
		echo "Available (built): $$built_tags"; \
		echo ""; \
		if [ -f docker/.env ]; then \
			current=$$(grep -E '^PHOTONARIUM_TAG=' docker/.env 2>/dev/null | cut -d= -f2); \
			if [ -n "$$current" ]; then \
				if docker image inspect $(IMAGE_NAME):$$current >/dev/null 2>&1; then \
					echo "Current selection: $$current"; \
				else \
					echo "Current selection: $$current $(CYAN)(warning: image not found)$(RESET)"; \
				fi; \
			else \
				echo "Current selection: latest (default)"; \
			fi; \
		else \
			echo "Current selection: latest (default)"; \
		fi; \
	fi
else
	@if docker image inspect $(IMAGE_NAME):$(TAG) >/dev/null 2>&1; then \
		if [ -f docker/.env ]; then \
			if grep -q '^PHOTONARIUM_TAG=' docker/.env; then \
				sed -i 's/^PHOTONARIUM_TAG=.*/PHOTONARIUM_TAG=$(TAG)/' docker/.env; \
			else \
				echo "PHOTONARIUM_TAG=$(TAG)" >> docker/.env; \
			fi; \
		else \
			echo "PHOTONARIUM_TAG=$(TAG)" > docker/.env; \
		fi; \
		echo "Selected: $(IMAGE_NAME):$(TAG)"; \
		echo "Run 'make up' to start the container."; \
	else \
		echo "Error: Image '$(IMAGE_NAME):$(TAG)' not found."; \
		echo ""; \
		built_tags="$(BUILT_TAGS)"; \
		if [ -n "$$built_tags" ]; then \
			echo "Available (built): $$built_tags"; \
		else \
			echo "No images built yet. Run 'make build' first."; \
		fi; \
		exit 1; \
	fi
endif

test:  ## Run container smoke test (health check)
	@echo "Starting container..."
	@docker compose -f $(COMPOSE_FILE) up -d
	@echo "Waiting for startup (30s)..."
	@sleep 30
	@echo "Checking health endpoint..."
	@curl -sf http://localhost:5000/api/health && echo " - Health check passed" || \
		(echo " - Health check FAILED"; docker compose -f $(COMPOSE_FILE) down; exit 1)
	@docker compose -f $(COMPOSE_FILE) down
	@echo "Smoke test completed successfully."

test-photos:  ## Run integration test with sample photos
	@echo "Starting container with test photos..."
	PHOTOS_PATH=$(TEST_PHOTOS) docker compose -f $(COMPOSE_FILE) -f $(PHOTOS_OVERLAY) up -d
	@echo "Waiting for startup (15s)..."
	@sleep 15
	@echo "Checking health endpoint..."
	@curl -sf http://localhost:5000/api/health && echo " - Health check passed" || \
		(echo " - Health check FAILED"; docker compose -f $(COMPOSE_FILE) -f $(PHOTOS_OVERLAY) down; exit 1)
	@echo ""
	@echo "Container running with test photos at http://localhost:5000"
	@echo "Run 'make logs' to follow output, 'make down' to stop."

up:  ## Start container (docker compose up -d)
	docker compose -f $(COMPOSE_FILE) up -d

up-photos:  ## Start container with photos mount (set PHOTOS_PATH first)
	@if [ -z "$(PHOTOS_PATH)" ]; then \
		echo "Error: PHOTOS_PATH not set."; \
		echo "Usage: PHOTOS_PATH=/path/to/photos make up-photos"; \
		exit 1; \
	fi
	PHOTOS_PATH=$(PHOTOS_PATH) docker compose -f $(COMPOSE_FILE) -f $(PHOTOS_OVERLAY) up -d

down:  ## Stop container (docker compose down)
	-docker compose -f $(COMPOSE_FILE) -f $(PHOTOS_OVERLAY) down 2>/dev/null
	-docker compose -f $(COMPOSE_FILE) down 2>/dev/null

logs:  ## Follow container logs
	docker compose -f $(COMPOSE_FILE) logs -f

shell:  ## Open bash shell in running container
	docker compose -f $(COMPOSE_FILE) exec photonarium bash

# =============================================================================
# Publishing
# =============================================================================

push-base:  ## Push base images to DockerHub
	docker tag $(BASE_IMAGE):x64 $(BASE_REPO):x64
	docker push $(BASE_REPO):x64
	docker tag $(BASE_IMAGE):arm64 $(BASE_REPO):arm64
	docker push $(BASE_REPO):arm64

push-latest:  ## Push latest/cpu image to DockerHub
	docker tag $(IMAGE_NAME):latest $(DOCKER_REPO):latest
	docker push $(DOCKER_REPO):latest
	docker tag $(IMAGE_NAME):cpu $(DOCKER_REPO):cpu
	docker push $(DOCKER_REPO):cpu

push-cu118:  ## Push CUDA 11.8 image to DockerHub
	docker tag $(IMAGE_NAME):cu118 $(DOCKER_REPO):cu118
	docker push $(DOCKER_REPO):cu118

push-cu126:  ## Push CUDA 12.6 image to DockerHub
	docker tag $(IMAGE_NAME):cu126 $(DOCKER_REPO):cu126
	docker push $(DOCKER_REPO):cu126

push-cu128:  ## Push CUDA 12.8 image to DockerHub
	docker tag $(IMAGE_NAME):cu128 $(DOCKER_REPO):cu128
	docker push $(DOCKER_REPO):cu128

push-intel:  ## Push Intel iGPU image to DockerHub
	docker tag $(IMAGE_NAME):intel $(DOCKER_REPO):intel
	docker push $(DOCKER_REPO):intel

push-arm64:  ## Push ARM64 image to DockerHub
	docker tag $(IMAGE_NAME):arm64 $(DOCKER_REPO):arm64
	docker push $(DOCKER_REPO):arm64

push: push-latest push-cu118 push-cu126 push-cu128 push-intel push-arm64  ## Push all images to DockerHub
	@echo "Done. View at: https://hub.docker.com/r/$(DOCKER_REPO)/tags"

# =============================================================================
# Cleanup
# =============================================================================

clean:  ## Remove variant images (keeps base images for faster rebuilds)
	@echo "Removing variant images..."
	-@docker rmi $(IMAGE_NAME):latest 2>/dev/null || true
	-@docker rmi $(IMAGE_NAME):cpu 2>/dev/null || true
	-@docker rmi $(IMAGE_NAME):cu118 2>/dev/null || true
	-@docker rmi $(IMAGE_NAME):cu126 2>/dev/null || true
	-@docker rmi $(IMAGE_NAME):cu128 2>/dev/null || true
	-@docker rmi $(IMAGE_NAME):intel 2>/dev/null || true
	-@docker rmi $(IMAGE_NAME):arm64 2>/dev/null || true
	-@docker rmi $(IMAGE_NAME):$(VERSION)-cpu 2>/dev/null || true
	-@docker rmi $(IMAGE_NAME):$(VERSION)-cu118 2>/dev/null || true
	-@docker rmi $(IMAGE_NAME):$(VERSION)-cu126 2>/dev/null || true
	-@docker rmi $(IMAGE_NAME):$(VERSION)-cu128 2>/dev/null || true
	-@docker rmi $(IMAGE_NAME):$(VERSION)-intel 2>/dev/null || true
	-@docker rmi $(IMAGE_NAME):$(VERSION)-arm64 2>/dev/null || true
	@rm -f $(CPU_MARKER) $(CU118_MARKER) $(CU126_MARKER) $(CU128_MARKER) $(INTEL_MARKER) $(ARM64_MARKER)
	@echo "Done. Base images retained for faster rebuilds."

clean-all:  ## Remove all images, markers, and prune Docker build cache
	@echo "Removing all Photonarium images..."
	-@docker rmi $(BASE_IMAGE):x64 2>/dev/null || true
	-@docker rmi $(BASE_IMAGE):arm64 2>/dev/null || true
	-@docker rmi $(BASE_IMAGE):latest 2>/dev/null || true
	-@docker rmi $(IMAGE_NAME):latest 2>/dev/null || true
	-@docker rmi $(IMAGE_NAME):cpu 2>/dev/null || true
	-@docker rmi $(IMAGE_NAME):cu118 2>/dev/null || true
	-@docker rmi $(IMAGE_NAME):cu126 2>/dev/null || true
	-@docker rmi $(IMAGE_NAME):cu128 2>/dev/null || true
	-@docker rmi $(IMAGE_NAME):intel 2>/dev/null || true
	-@docker rmi $(IMAGE_NAME):arm64 2>/dev/null || true
	-@docker rmi $(IMAGE_NAME):$(VERSION)-cpu 2>/dev/null || true
	-@docker rmi $(IMAGE_NAME):$(VERSION)-cu118 2>/dev/null || true
	-@docker rmi $(IMAGE_NAME):$(VERSION)-cu126 2>/dev/null || true
	-@docker rmi $(IMAGE_NAME):$(VERSION)-cu128 2>/dev/null || true
	-@docker rmi $(IMAGE_NAME):$(VERSION)-intel 2>/dev/null || true
	-@docker rmi $(IMAGE_NAME):$(VERSION)-arm64 2>/dev/null || true
	@rm -rf $(MARKER_DIR)
	docker builder prune -f
	@echo "Done."

prune:  ## Remove non-release versioned images (intermediate commits)
	@echo "Pruning non-release $(IMAGE_NAME) images..."
	@# Non-release versions have a git-describe suffix: -<N>-g<hex>
	@# e.g. v1.1.4-beta.14-3-gdd3bbbe vs release v1.1.4-beta.14
	@all_tags=$$(docker images $(IMAGE_NAME) --format '{{.Tag}}' 2>/dev/null \
		| grep '^v' | sort -u); \
	if [ -z "$$all_tags" ]; then \
		echo "No versioned images found."; \
	else \
		removed=0; \
		for tag in $$all_tags; do \
			base=$$(echo "$$tag" | sed -E 's/-(cpu|cu118|cu126|cu128|intel|arm64)$$//'); \
			if echo "$$base" | grep -qE -- '-[0-9]+-g[0-9a-f]+$$'; then \
				echo "  $(IMAGE_NAME):$$tag"; \
				docker rmi "$(IMAGE_NAME):$$tag" 2>/dev/null || true; \
				removed=$$((removed + 1)); \
			fi; \
		done; \
		if [ "$$removed" -eq 0 ]; then \
			echo "No non-release images found."; \
		else \
			echo "Removed $$removed non-release tag(s)."; \
		fi; \
	fi
	@echo "Pruning dangling images..."
	@docker image prune -f
	@echo "Pruning build cache..."
	@docker builder prune -f
	@echo "Done."

prune-all: prune  ## Also remove old releases, keeping KEEP=2 most recent
	@echo "Pruning old release versions (keeping $(KEEP))..."
	@all_tags=$$(docker images $(IMAGE_NAME) --format '{{.Tag}}' 2>/dev/null \
		| grep '^v' | sort -u); \
	if [ -z "$$all_tags" ]; then \
		echo "No release images remaining."; \
	else \
		base_versions=$$(docker images $(IMAGE_NAME) \
			--format '{{.CreatedAt}}\t{{.Tag}}' 2>/dev/null \
			| grep '	v' | sort -r \
			| awk -F'\t' '{print $$2}' \
			| sed -E 's/-(cpu|cu118|cu126|cu128|intel|arm64)$$//' \
			| awk '!seen[$$0]++'); \
		total=$$(echo "$$base_versions" | wc -l); \
		if [ "$$total" -le "$(KEEP)" ]; then \
			echo "Only $$total release(s) found, nothing to prune."; \
		else \
			keep=$$(echo "$$base_versions" | head -n $(KEEP)); \
			remove=$$(echo "$$base_versions" | tail -n +$$(($(KEEP) + 1))); \
			echo "Keeping:"; \
			echo "$$keep" | while read -r ver; do echo "  $$ver"; done; \
			echo "Removing:"; \
			removed=0; \
			for ver in $$remove; do \
				matching=$$(echo "$$all_tags" \
					| grep -E "^$${ver}(-(cpu|cu118|cu126|cu128|intel|arm64))?$$"); \
				for tag in $$matching; do \
					echo "  $(IMAGE_NAME):$$tag"; \
					docker rmi "$(IMAGE_NAME):$$tag" 2>/dev/null || true; \
					removed=$$((removed + 1)); \
				done; \
			done; \
			echo "Removed $$removed release tag(s)."; \
		fi; \
	fi
	@docker image prune -f
	@echo "Done."

# =============================================================================
# Help
# =============================================================================

help:  ## Show this help
	@echo ""
	@echo "Photonarium Docker Build"
	@echo "========================"
	@echo ""
	@echo "Usage: make [target]"
	@echo ""
	@echo "$(CYAN)Setup (run once):$(RESET)"
	@grep -E '^models:.*##' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*##"}; {printf "  $(CYAN)%-14s$(RESET) %s\n", $$1, $$2}'
	@echo ""
	@echo "$(CYAN)Base images:$(RESET)"
	@grep -E '^base-.*:.*##' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*##"}; {printf "  $(CYAN)%-14s$(RESET) %s\n", $$1, $$2}'
	@echo ""
	@echo "$(CYAN)Build targets:$(RESET)"
	@grep -E '^build.*:.*##' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*##"}; {printf "  $(CYAN)%-14s$(RESET) %s\n", $$1, $$2}'
	@echo ""
	@echo "  all-x64         Build all x64 variants"
	@echo "  all-images      Build all image variants"
	@echo ""
	@echo "$(CYAN)Runtime targets:$(RESET)"
	@echo "  $(CYAN)use            $(RESET) Select image variant (make use TAG=cu126)"
	@grep -E '^(test|test-photos|up|up-photos|down|logs|shell):.*##' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*##"}; {printf "  $(CYAN)%-14s$(RESET) %s\n", $$1, $$2}'
	@echo ""
	@echo "$(CYAN)Publishing:$(RESET)"
	@grep -E '^push.*:.*##' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*##"}; {printf "  $(CYAN)%-14s$(RESET) %s\n", $$1, $$2}'
	@echo ""
	@echo "$(CYAN)Cleanup:$(RESET)"
	@grep -E '^(prune|prune-all|clean).*:.*##' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*##"}; {printf "  $(CYAN)%-14s$(RESET) %s\n", $$1, $$2}'
	@echo ""
	@echo "Examples:"
	@echo "  make models       Download models (run once)"
	@echo "  make build-cpu    Build CPU image for NAS deployment"
	@echo "  make build-cu126  Build CUDA 12.6 image for RTX 30xx/40xx"
	@echo "  make build-arm64  Build ARM64 image for Raspberry Pi/Apple Silicon"
	@echo "  make all-images   Build all image variants"
	@echo "  make prune        Remove intermediate commit images"
	@echo "  make push         Push all built images to DockerHub"
	@echo "  make up           Start the container"
	@echo "  make logs         Follow container output"
	@echo ""
	@echo "Dependency chain:"
	@echo "  config.py → models → base-x64 → build variants"
	@echo "  Code changes only rebuild the variant layer (~3MB)"
	@echo ""

.DEFAULT_GOAL := help
