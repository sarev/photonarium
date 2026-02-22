# Docker / NAS Deployment -- Overview

A plain English companion to `docker-nas-plan.md`. This document describes
what the Docker deployment is, what it costs, what you get, and how the
day-to-day workflow looks. The plan document covers implementation details.

---

## What is it?

A way to run Photonarium on a NAS (Synology, QNAP, Unraid, TrueNAS) or any
Linux server, packaged as a Docker container. You get the same Photonarium
features -- semantic search, face recognition, duplicate detection, quality
scoring, import -- but running headless on a machine that's always on, without
needing a desktop or monitor.

---

## What's different from the desktop version?

The desktop version runs on your PC with a native folder picker, "open in file
manager" button, and direct access to the local filesystem. The Docker version
runs headless on a server, so:

- **No folder picker, no "reveal in file manager", no "add local folder"
  button.** These features are hidden. Photo folders are configured once in
  a docker-compose file, not through the UI.
- **Import is the primary way to add new photos.** You upload photos through
  the web UI (from your phone, tablet, or desktop browser) and they land in a
  managed catalogue folder, organised by date.
- **Existing photo libraries are mounted read-only.** If you already have
  photos on your NAS (e.g., synced from Apple Photos, Google Photos, Dropbox),
  you mount those folders into the container and Photonarium indexes them
  without touching the originals. Trash and rotate are disabled for read-only
  libraries.
- **Scheduled rescans run automatically.** Since many NAS users have sync apps
  that continuously add photos to folders, the Docker version rescans for new
  photos on a timer (default: every hour). The timer is smart -- if a scan or
  import is already running, it waits until processing finishes, then starts
  the countdown again. No scans pile up or thrash the system.

Everything else -- the gallery, fullscreen viewer, search, face tagging,
groups, settings -- works identically.

---

## Storage layout

The container uses three volume mounts on the host:

| Volume | What it holds | Typical size | Storage type |
|--------|---------------|--------------|--------------|
| `/config` | Database, thumbnails, ML models, settings, trash | 15-20 GB for ~65k images | Local disk (SSD ideal, HDD acceptable) |
| `/catalogue` | Imported photos, organised into `YYYY/YYYY-MM-DD/` folders | Depends on how many photos you import | Any storage (HDD, NAS share) |
| `/photos` | Existing photo libraries (mounted read-only) | Your existing library size | Any storage (NFS, SMB, HDD) |

Users may have `/catalogue` only (purely import-based workflow), `/photos`
only (indexing existing libraries), or both.

### Why is /config so large?

For a medium-sized library (~65k images), `/config` holds:

- ~1 GB: SQLite database (metadata, embeddings, face data)
- ~5-10 GB: ML models (OpenCLIP, BLIP, LAION, NIMA -- copied from the image
  on first run)
- ~6 GB: Thumbnail cache (200px + 400px for every image)

The database benefits most from SSD. Thumbnails and models are read
sequentially and tolerate spinning disk fine. If SSD space is tight, HDD
works -- the main impact is slower thumbnail loading on first view and
slightly slower startup.

---

## Building the container images

Multiple image variants are built from the same Dockerfile using build arguments:

| Variant | Tag | Target Hardware |
|---------|-----|-----------------|
| CPU-only | `:latest`, `:cpu` | Most NAS devices (default) |
| CUDA 11.8 | `:cu118` | Older NVIDIA GPUs (GTX 10xx, RTX 20xx) |
| CUDA 12.6 | `:cu126` | Modern NVIDIA GPUs (RTX 30xx, 40xx) |
| CUDA 12.8 | `:cu128` | RTX 50xx / Blackwell (speculative, needs testing) |
| Intel iGPU | `:intel` | Celeron/Atom NAS with integrated graphics |

### What happens during a build

1. Pulls a base Python image (~150 MB)
2. Installs system libraries (image processing, ML dependencies)
3. Installs PyTorch (~800 MB for CPU, ~1.6 GB for CUDA)
4. Installs all other Python dependencies
5. Copies the Photonarium source code
6. Downloads all ML models and bakes them into the image (~5-10 GB download)
7. Packages everything into a single image

### Build commands

Builds are orchestrated via GNU Make (available on Linux/macOS/WSL2):

```bash
# CPU-only (most NAS users) -- ~3.5 GB image
make build

# NVIDIA CUDA variants -- ~4.3 GB images
make build-cu118    # CUDA 11.8 (GTX 10xx, RTX 20xx)
make build-cu126    # CUDA 12.6 (RTX 30xx, 40xx)
make build-cu128    # CUDA 12.8 (RTX 50xx / Blackwell) [speculative, needs testing]

# Intel iGPU variant (Celeron/Atom NAS devices with integrated graphics)
make build-intel

# Build all variants
make all-images

# See all available targets
make help
```

A full build
from scratch takes 10-20 minutes depending on internet speed. Rebuilds after
code-only changes are much faster because Docker caches the expensive
dependency and model layers.

### When you need to rebuild

- **New Photonarium release:** Pull the latest source, rebuild. Docker's layer
  cache means only the app code layer changes -- dependencies and models are
  reused if unchanged.
- **Changing OpenCLIP or BLIP model:** Models are baked in at build time, so a
  different model choice requires a rebuild.
- **Day-to-day usage:** No rebuilds needed. The container just runs.

### Future: pre-built images

If Photonarium is published to Docker Hub or GitHub Container Registry, users
would just `docker pull photonarium:latest` instead of building locally. This
requires CI/CD infrastructure (GitHub Actions) and is not part of the initial
plan.

---

## Running the container

### First-time setup

1. Copy `.env.example` to `.env` and edit your paths and user/group IDs
2. Run `docker compose up -d`
3. Open `http://nas-ip:5000` in a browser
4. Photos in mounted `/photos` folders need to be registered once via the
   docker-compose `command:` field (e.g., `--add-folder /photos --scan`)
5. Import new photos via the web UI

### What happens on first start

The entrypoint script:
- Creates a system user matching your PUID/PGID (so file permissions work)
- Copies all ML models from the image into `/config` (one-time, ~5-10 GB)
- Generates a default `photonarium.yml` config (headless mode, catalogue
  path, hourly rescan)
- Starts Photonarium

First start takes a minute or two as models are copied. Subsequent starts
are fast.

### Updating

```bash
# Rebuild with new source code (or docker pull when pre-built images exist)
docker compose down
docker compose up -d --build
```

The database, photos, catalogue, and config persist in the mounted volumes.
Models in `/config` persist too -- they're only copied from the image if
missing, so an update doesn't re-copy them. If a new version ships updated
models, delete the old model files in `/config` and restart to get fresh
copies.

---

## GPU acceleration

Most NAS users will run CPU-only. ML processing (face detection, embeddings,
quality scoring) is slower but works fine -- it just means the initial scan of
a large library takes longer (hours rather than minutes for 65k images).

Two GPU paths are supported:

### NVIDIA CUDA

For users with a discrete NVIDIA GPU (common on Unraid, custom server builds).
Requires:
- NVIDIA Container Toolkit installed on the host
- The appropriate CUDA image variant:
  - `:cu118` for older GPUs (GTX 10xx, RTX 20xx)
  - `:cu126` for modern GPUs (RTX 30xx, 40xx)
  - `:cu128` for RTX 50xx / Blackwell (speculative, needs testing)
- A compose override file (`hwaccel.cuda.yml`) for GPU device mapping

### Intel iGPU

Potentially more relevant for NAS users, since many Synology and QNAP devices
use Intel Celeron/Atom CPUs with integrated graphics. Uses Intel Extension for
PyTorch (IPEX). Requires:
- The `:intel` image variant
- Passing `/dev/dri` into the container
- A compose override file (`hwaccel.intel.yml`)

---

## NAS platform compatibility

### Tested / expected to work

| Platform | How to deploy | Notes |
|----------|---------------|-------|
| **Synology** (DSM 7+) | Container Manager or Portainer | Most popular NAS brand. PUID/PGID must match Synology user. |
| **QNAP** | Container Station | Standard docker-compose deployment. |
| **Unraid** (6.12+) | Community Apps or Compose Manager plugin | Config on cache drive if space permits. GPU acceleration compose must be inlined (Unraid limitation). |
| **TrueNAS Scale** | TrueNAS Apps or Docker Compose | Datasets for config, catalogue, and photos. |
| **Generic Linux** | Docker Compose | Any Linux server with Docker installed. |

### Proxmox (virtualisation host)

Three options, from heaviest to lightest:

1. **VM + Docker**: Standard Linux VM, Docker inside, docker-compose
   deployment. Most reliable, especially for NVIDIA GPU passthrough.
2. **LXC + Docker**: Lighter weight but Docker-in-LXC can be fragile. Works
   well for single-container apps when `nesting=1` is enabled.
3. **LXC native (no Docker)**: Install Python dependencies directly, run
   `app.py`. Lightest option, avoids Docker-in-LXC issues entirely.
   Photonarium's single-process design makes this straightforward.

### Architecture support

- **x86_64 (Intel/AMD):** Supported from day one. Covers the vast majority
  of NAS devices.
- **ARM64 (Raspberry Pi, some Synology):** Deferred. The base image supports
  arm64 natively, but PyTorch ARM64 wheels and build infrastructure need
  work.

---

## Reverse proxy

Works behind nginx, Caddy, or Traefik. Unlike Immich and PhotoPrism,
Photonarium can work on a sub-path (e.g., `nas.local/photos`) rather than
requiring a dedicated domain. Key requirements:

- Increase upload size limit (for photo imports via the web UI)
- Increase timeouts to 600s (for long-running ML operations)
- Forward standard proxy headers (Host, X-Real-IP, X-Forwarded-Proto)

---

## Backups

### What to back up

1. `/config/photonarium.db` -- the database (critical, ~1 GB)
2. `/config/photonarium.yml` -- settings
3. `/catalogue` -- imported photos (if not backed up separately)

### What can be regenerated

- Thumbnails (~6 GB) -- regenerated by `--generate-thumbnails`
- ML models (~5-10 GB) -- re-copied from the image on next start if deleted

### How to back up safely

The database uses SQLite WAL mode, so you can't just copy the `.db` file
while the container is running. Either stop the container first, or use
SQLite's built-in online backup:

```bash
docker exec photonarium \
    sqlite3 /config/photonarium.db ".backup /config/photonarium-backup.db"
```

Automated scheduled backups (PhotoPrism-style) are a planned follow-up.

---

## Limitations and trade-offs

### Compared to the desktop version

- No native folder picker or "open in file manager"
- No real-time filesystem watching (hourly rescan instead)
- Initial scan of large libraries is slow on NAS CPUs (no GPU by default)

### Compared to Immich

- **Simpler:** One container vs four. No Postgres, no Redis, no separate ML
  worker. Easier to set up, back up, and understand.
- **Offline-first:** All models baked in, no internet needed after initial
  image download. Immich downloads models on first use.
- **No mobile app:** Immich has native iOS/Android apps with background upload.
  Photonarium uses the mobile web browser.
- **No remote ML:** Immich can run its ML container on a separate GPU machine.
  Photonarium's ML is in-process, so it runs where the app runs.
- **Single-user:** No multi-user accounts or sharing (same as desktop).

### Compared to PhotoPrism

- **Simpler:** One container vs two (no MariaDB). SQLite with WAL mode handles
  concurrent reads well enough for a single-user app.
- **Offline-first:** Same advantage as above.
- **No video:** Photonarium is image-only. PhotoPrism handles video
  transcoding.
- **No WebDAV:** PhotoPrism exposes WebDAV for uploads. Photonarium uses its
  own Import feature.

---

## Costs

### Disk space

- Docker image: ~3.5 GB (CPU/Intel) or ~4.3 GB (CUDA variants)
- `/config` volume: 15-20 GB for ~65k images (scales with library size)
- `/catalogue` volume: depends on how many photos you import

### Memory

- Minimum: 4 GB RAM
- Recommended: 8 GB+ for comfortable indexing alongside NAS operations

### CPU

- Minimum: 2 cores
- Recommended: 4+ cores (ML processing is CPU-intensive)

### Build time

- First build: 10-20 minutes (mostly downloading dependencies and models)
- Rebuilds after code changes: 1-3 minutes (Docker layer caching)

### Initial scan time (CPU-only)

Very rough estimates for CPU-only on a typical NAS (4-core Celeron):
- 10k images: ~1-2 hours
- 65k images: ~6-12 hours
- Best done overnight after first setup
