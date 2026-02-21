# =============================================================================
# Photonarium Docker Build
#
# Usage:
#   make              Show available targets (help)
#   make build        Build CPU-only image (default for most NAS devices)
#   make build-cu126  Build CUDA 12.6 image (RTX 30xx, 40xx)
#   make test         Run container smoke test
#
# Requires: GNU Make, Docker, docker-compose
# =============================================================================

IMAGE_NAME := photonarium
VERSION    := $(shell git describe --tags --always 2>/dev/null || echo dev)
GIT_COMMIT := $(shell git rev-parse --short HEAD 2>/dev/null || echo unknown)
BUILD_DATE := $(shell date -u +%Y-%m-%dT%H:%M:%SZ)

# PyTorch wheel index URLs
TORCH_CPU   := https://download.pytorch.org/whl/cpu
TORCH_CU118 := https://download.pytorch.org/whl/cu118
TORCH_CU126 := https://download.pytorch.org/whl/cu126
TORCH_CU128 := https://download.pytorch.org/whl/cu128

# Compose file location
COMPOSE_FILE := docker/docker-compose.yml
PHOTOS_OVERLAY := docker/docker-compose.photos.yml

# Tutorial examples for testing
TEST_PHOTOS := $(CURDIR)/tools/mktutorial/examples

# HuggingFace token for faster model downloads (optional)
# Set HF_TOKEN in your environment or ~/.cache/huggingface/token
HF_TOKEN_ARG := $(if $(HF_TOKEN),--build-arg HF_TOKEN=$(HF_TOKEN),)

# Colours for help output (disabled if not a terminal)
CYAN  := $(shell tput setaf 6 2>/dev/null || echo "")
RESET := $(shell tput sgr0 2>/dev/null || echo "")

# DockerHub repository (override with: make push DOCKER_REPO=myuser/photonarium)
DOCKER_REPO := 7thsw/photonarium

# Variant tags we publish (excludes versioned tags like v1.1.0-cu126)
VARIANT_TAGS := latest cpu cu118 cu126 cu128 intel

.PHONY: build build-cu118 build-cu126 build-cu128 build-intel all-images \
        use test test-photos up up-photos down logs shell push clean clean-all help

# =============================================================================
# Build targets
# =============================================================================

build:  ## Build CPU-only image (default, ~3.5 GB)
	docker build \
		--build-arg TORCH_INDEX=$(TORCH_CPU) \
		--build-arg VERSION=$(VERSION) \
		--build-arg GIT_COMMIT=$(GIT_COMMIT) \
		--build-arg BUILD_DATE=$(BUILD_DATE) \
		--build-arg VARIANT=cpu \
		$(HF_TOKEN_ARG) \
		-t $(IMAGE_NAME):latest \
		-t $(IMAGE_NAME):cpu \
		-t $(IMAGE_NAME):$(VERSION) \
		-f docker/Dockerfile .

build-cu118:  ## Build CUDA 11.8 image (GTX 10xx, RTX 20xx)
	docker build \
		--build-arg TORCH_INDEX=$(TORCH_CU118) \
		--build-arg VERSION=$(VERSION) \
		--build-arg GIT_COMMIT=$(GIT_COMMIT) \
		--build-arg BUILD_DATE=$(BUILD_DATE) \
		--build-arg VARIANT=cu118 \
		$(HF_TOKEN_ARG) \
		-t $(IMAGE_NAME):cu118 \
		-t $(IMAGE_NAME):$(VERSION)-cu118 \
		-f docker/Dockerfile .

build-cu126:  ## Build CUDA 12.6 image (RTX 30xx, 40xx)
	docker build \
		--build-arg TORCH_INDEX=$(TORCH_CU126) \
		--build-arg VERSION=$(VERSION) \
		--build-arg GIT_COMMIT=$(GIT_COMMIT) \
		--build-arg BUILD_DATE=$(BUILD_DATE) \
		--build-arg VARIANT=cu126 \
		$(HF_TOKEN_ARG) \
		-t $(IMAGE_NAME):cu126 \
		-t $(IMAGE_NAME):$(VERSION)-cu126 \
		-f docker/Dockerfile .

build-cu128:  ## Build CUDA 12.8 image (RTX 50xx / Blackwell) [speculative]
	docker build \
		--build-arg TORCH_INDEX=$(TORCH_CU128) \
		--build-arg VERSION=$(VERSION) \
		--build-arg GIT_COMMIT=$(GIT_COMMIT) \
		--build-arg BUILD_DATE=$(BUILD_DATE) \
		--build-arg VARIANT=cu128 \
		$(HF_TOKEN_ARG) \
		-t $(IMAGE_NAME):cu128 \
		-t $(IMAGE_NAME):$(VERSION)-cu128 \
		-f docker/Dockerfile .

build-intel:  ## Build Intel iGPU image (IPEX for Celeron/Atom NAS)
	docker build \
		--build-arg TORCH_INDEX=$(TORCH_CPU) \
		--build-arg INSTALL_IPEX=1 \
		--build-arg VERSION=$(VERSION) \
		--build-arg GIT_COMMIT=$(GIT_COMMIT) \
		--build-arg BUILD_DATE=$(BUILD_DATE) \
		--build-arg VARIANT=intel \
		$(HF_TOKEN_ARG) \
		-t $(IMAGE_NAME):intel \
		-t $(IMAGE_NAME):$(VERSION)-intel \
		-f docker/Dockerfile .

all-images: build build-cu118 build-cu126 build-cu128 build-intel  ## Build all image variants

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
	@echo "Waiting for startup (10s)..."
	@sleep 10
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

push:  ## Push all built images to DockerHub
	@if ! git describe --tags --exact-match HEAD >/dev/null 2>&1; then \
		echo "Warning: HEAD is not a tagged release."; \
		echo "  Current version: $(VERSION)"; \
		echo "  Pushing development builds to DockerHub is not recommended."; \
		echo ""; \
		read -p "Push anyway? [y/N] " confirm; \
		if [ "$$confirm" != "y" ] && [ "$$confirm" != "Y" ]; then \
			echo "Aborted."; \
			exit 1; \
		fi; \
	fi
	@echo "Pushing to $(DOCKER_REPO)..."
	@found=0; \
	for tag in $(VARIANT_TAGS); do \
		if docker image inspect $(IMAGE_NAME):$$tag >/dev/null 2>&1; then \
			echo ""; \
			echo "Tagging and pushing: $$tag"; \
			docker tag $(IMAGE_NAME):$$tag $(DOCKER_REPO):$$tag; \
			docker push $(DOCKER_REPO):$$tag; \
			found=1; \
		fi; \
	done; \
	if [ $$found -eq 0 ]; then \
		echo "No images to push. Build first with 'make build' or 'make build-cu126'."; \
		exit 1; \
	fi
	@echo ""
	@echo "Done. View at: https://hub.docker.com/r/$(DOCKER_REPO)/tags"

# =============================================================================
# Cleanup
# =============================================================================

clean:  ## Remove all built Photonarium images
	@echo "Removing Photonarium images..."
	-@docker rmi $(IMAGE_NAME):latest 2>/dev/null || true
	-@docker rmi $(IMAGE_NAME):cpu 2>/dev/null || true
	-@docker rmi $(IMAGE_NAME):cu118 2>/dev/null || true
	-@docker rmi $(IMAGE_NAME):cu126 2>/dev/null || true
	-@docker rmi $(IMAGE_NAME):cu128 2>/dev/null || true
	-@docker rmi $(IMAGE_NAME):intel 2>/dev/null || true
	-@docker rmi $(IMAGE_NAME):$(VERSION) 2>/dev/null || true
	-@docker rmi $(IMAGE_NAME):$(VERSION)-cu118 2>/dev/null || true
	-@docker rmi $(IMAGE_NAME):$(VERSION)-cu126 2>/dev/null || true
	-@docker rmi $(IMAGE_NAME):$(VERSION)-cu128 2>/dev/null || true
	-@docker rmi $(IMAGE_NAME):$(VERSION)-intel 2>/dev/null || true
	@echo "Done."

clean-all: clean  ## Remove images and prune Docker build cache
	docker builder prune -f

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
	@echo "$(CYAN)Build targets:$(RESET)"
	@grep -E '^build.*:.*##' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*##"}; {printf "  $(CYAN)%-14s$(RESET) %s\n", $$1, $$2}'
	@echo ""
	@echo "  all-images      Build all image variants"
	@echo ""
	@echo "$(CYAN)Runtime targets:$(RESET)"
	@echo "  $(CYAN)use            $(RESET) Select image variant (make use TAG=cu126)"
	@grep -E '^(test|test-photos|up|up-photos|down|logs|shell):.*##' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*##"}; {printf "  $(CYAN)%-14s$(RESET) %s\n", $$1, $$2}'
	@echo ""
	@echo "$(CYAN)Publishing:$(RESET)"
	@grep -E '^push:.*##' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*##"}; {printf "  $(CYAN)%-14s$(RESET) %s\n", $$1, $$2}'
	@echo ""
	@echo "$(CYAN)Cleanup:$(RESET)"
	@grep -E '^clean.*:.*##' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*##"}; {printf "  $(CYAN)%-14s$(RESET) %s\n", $$1, $$2}'
	@echo ""
	@echo "Examples:"
	@echo "  make build          Build CPU image for NAS deployment"
	@echo "  make build-cu126    Build CUDA 12.6 image for RTX 30xx/40xx"
	@echo "  make push           Push all built images to DockerHub"
	@echo "  make up             Start the container"
	@echo "  make logs           Follow container output"
	@echo ""

.DEFAULT_GOAL := help
