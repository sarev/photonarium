# Docker / NAS Deployment Plan

Implementation plan for running Photonarium as a Docker container on NAS
devices (Synology, QNAP, Unraid, TrueNAS) and generic Linux servers.

**See also:** `docker-nas-overview.md` for a plain English overview of costs,
benefits, workflow, and limitations.

---

## Design Decisions

### Single-container architecture

Photonarium is a single-process Python app with SQLite (no Postgres, Redis, or
worker queues). This maps naturally to a single Docker container, following the
LinuxServer.io pattern rather than the Immich multi-container pattern (4
containers: server, ML, PostgreSQL, Redis) or PhotoPrism's 2-container setup
(app + MariaDB). A single container is simpler to deploy, back up, and reason
about - important for home NAS users who aren't Docker experts.

**SQLite concurrency note:** PhotoPrism strongly recommends MariaDB over SQLite
for production, because SQLite locks the entire database on writes -- browsing
while indexing can cause timeout errors. Photonarium uses WAL mode which allows
concurrent reads during writes, significantly reducing this problem. However,
we should document that very large indexing jobs (initial scan of 100k+ images)
may still cause brief UI slowdowns, and recommend scheduling heavy scans during
off-hours.

### Models baked into the image

HuggingFace models (OpenCLIP ~350MB, BLIP ~2GB) live in `~/.cache/huggingface/`
which is outside `data_dir`. Two options exist:

- **Option A: Bake into image** - Run `download_models.py` during `docker build`.
  Image is ~3.5GB but "just works" with no internet on first run.
- **Option B: Volume-mount cache** - Smaller image, but requires the user to
  either run a download step or have internet on first start.

**Decision: Option A (bake in).** The target audience (NAS users) values
simplicity over image size. PhotoPrism bakes its models in too. Immich takes
the opposite approach (downloads on first use into a Docker volume), which
requires internet access -- at odds with Photonarium's offline-first design.

**All models** that `download_models.py` downloads are baked into the image
during the build and copied to `/config` on first run. This includes the
HuggingFace models (OpenCLIP, BLIP), the LAION aesthetic head, and the NIMA
weights. At runtime, all models live in `/config` (the user's persistent
volume) -- the app reads them from `data_dir` as normal. If models were left
inside the image layer instead, they'd be inaccessible once gosu drops to the
PUID/PGID user, and any model the app couldn't find would require internet
access to download -- a non-starter for offline NAS deployments.

### Single multi-arch image

PhotoPrism publishes a single `photoprism/photoprism:latest` multi-arch image
for amd64, arm64, and armv7 -- no architecture-specific images needed. GPU
support is installed at runtime (`PHOTOPRISM_INIT: "tensorflow-gpu"`) rather
than baked into separate images. This avoids maintaining multiple Dockerfiles.

Photonarium should follow the single-image pattern for architecture (when
arm64 is added). For GPU, the build-arg approach (CPU vs CUDA PyTorch wheels)
is better since PyTorch CUDA adds ~800MB that CPU-only users shouldn't carry.

### Image variants

Five image variants are built from a single Dockerfile using build arguments,
managed via GNU Make:

| Variant | Tag | PyTorch Index | Target Hardware |
|---------|-----|---------------|-----------------|
| CPU-only | `:latest`, `:cpu` | `/whl/cpu` | Most NAS devices (default) |
| CUDA 11.8 | `:cu118` | `/whl/cu118` | Older NVIDIA (GTX 10xx, RTX 20xx) |
| CUDA 12.6 | `:cu126` | `/whl/cu126` | Modern NVIDIA (RTX 30xx, 40xx) |
| CUDA 12.8 | `:cu128` | `/whl/cu128` | RTX 50xx / Blackwell (speculative) |
| Intel iGPU | `:intel` | `/whl/cpu` + IPEX | Celeron/Atom NAS with iGPU |

Most NAS devices have no GPU. The default `:latest` image uses CPU-only PyTorch
(~800MB smaller than CUDA variants).

Note that many consumer NAS devices (Synology, QNAP) use Intel Celeron/Atom
CPUs with integrated graphics. The `:intel` variant uses Intel Extension for
PyTorch (IPEX) for iGPU acceleration, which may be more relevant to the NAS
audience than NVIDIA CUDA.

### PUID/PGID for NAS filesystem permissions

NAS platforms mount photo libraries with specific user/group ownership. The
container must run as the host user's UID/GID to avoid permission errors on
mounted volumes. This is the universal NAS convention (LinuxServer.io, Immich,
PhotoPrism all do it).

---

## Volume Layout

```
/config       Persistent application data (database, thumbnails, trash, config)
              Maps to: e.g. /volume1/docker/photonarium on Synology
/photos       Existing photo library (read-only or read-write)
              Maps to: e.g. /volume1/photos on Synology
/catalogue    Managed catalogue for imported photos (organised by date)
              Maps to: e.g. /volume1/photonarium-catalogue on Synology
```

**IMPORTANT: `/config` must be on local storage**, never on a network share
(NFS/SMB). SQLite does not work reliably over network filesystems -- concurrent
access can corrupt the database. Photo storage (`/photos`) and the catalogue
(`/catalogue`) can be on network shares or HDD arrays.

**Size expectations for `/config`:** This volume is larger than it sounds. For
a library of ~65k images, real-world sizes are roughly:
- Database: ~1 GB (grows with library size, benefits most from SSD)
- ML models: ~5-10 GB (depends on model choice, read on startup then cached
  in RAM -- does not benefit much from SSD)
- Thumbnails: ~6 GB (200px + 400px for every image, read-heavy but sequential
  -- tolerates HDD fine)
- LAION/NIMA weights: ~9 MB

That's **15-20 GB** for a medium-sized library, growing with collection size.
NAS SSD caches are often limited (e.g., 128-256 GB shared with other apps),
so recommending "put it all on SSD" isn't always practical. The database is
the only component that genuinely needs fast random I/O. Thumbnails and models
are read-sequentially and work fine on spinning disk.

For users with limited SSD space, document that `/config` can live on HDD
with acceptable performance. The main impact is slower thumbnail loading on
first view (before the in-memory LRU cache warms up) and slightly slower
startup. The database will still work on HDD -- it's just not as snappy for
large filter/sort operations.

In Docker, the catalogue volume is the primary way new photos enter the system
(via the Import feature in the UI). The `/photos` volume is for indexing
existing photo libraries that live elsewhere on the NAS. Users may have one or
both, depending on their workflow.

Inside `/config`:
```
/config/photonarium.yml            Configuration file
/config/photonarium.db             SQLite database
/config/.thumbnails/               Thumbnail cache (200px + 400px)
/config/trash/                     Trashed images (unless overridden)
/config/.cache/huggingface/        HuggingFace models (OpenCLIP, BLIP)
/config/.laion-aesthetic-head.pth  LAION aesthetic weights
/config/.nima-mobilenetv2-ava.pth  NIMA aesthetic weights
```

All ML models are copied from the image on first run. They persist in the
volume across container updates, so subsequent starts are fast. If a new
Photonarium version ships with updated models, the user can delete the old
model files and restart to get fresh copies from the image.

The `/photos` mount is the user's existing photo library. Photonarium indexes
it but does not modify originals (only trash moves files out). If the user
wants trash to go elsewhere, they can set `trash_dir` in the config.

**Read-only originals:** PhotoPrism has an explicit `PHOTOPRISM_READONLY` flag
for read-only originals. Photonarium should document that `/photos` can be
mounted read-only (`:ro` in compose) with the caveat that **trash and rotate
will not work** -- both modify files on disk (trash moves them, rotate
rewrites the JPEG). Import (copy-into-catalogue) writes to the catalogue
volume, not `/photos`, so it would still work. Consider adding a
`PHOTONARIUM_READONLY` env var that disables trash/rotate in the UI and hides
the relevant buttons (cleaner than silent failures or cryptic OS errors).

**Bind mounts only:** PhotoPrism explicitly warns against Docker named volumes
for user data (risk of data loss during container recreation). Document that
`/config` and `/photos` should always be bind mounts to host paths, never
anonymous or named Docker volumes. This ensures data survives `docker compose
down && docker compose up`.

**Multiple photo libraries:** Mount additional libraries as subdirectories:
```yaml
volumes:
  - /nas/photos:/photos/main
  - /nas/holidays:/photos/holidays
  - /mnt/external:/photos/external:ro    # read-only external drive
```
Register each via `--add-folder /photos/main --add-folder /photos/holidays`
etc. No code changes needed -- this works with the existing design.

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PUID` | 1000 | Run as this user ID (match NAS user) |
| `PGID` | 1000 | Run as this group ID (match NAS group) |
| `PHOTONARIUM_PORT` | 5000 | Server port (passed as --port) |

Existing env vars that already work:
- `PHOTONARIUM_CONFIG` - config file path (auto-set to `/config/photonarium.yml`)
- `PHOTONARIUM_DB` - database path (auto-set to `/config/photonarium.db`)
- `PHOTONARIUM_THUMBNAILS` - thumbnail dir (auto-set to `/config/.thumbnails`)

---

## Implementation Tasks

### Phase 1: Backend Changes (small, targeted)

#### 1a. Add a `/api/health` endpoint

A lightweight health check endpoint for Docker's `HEALTHCHECK` directive.
Returns 200 if the server is running and the database is accessible.

**File:** `app.py`
**Change:** Add one route:
```python
@app.route('/api/health')
def health():
    """Health check for Docker/monitoring. Returns 200 if DB is accessible."""
    try:
        db = get_db()
        db.conn.execute('SELECT 1')
        return success_response({'status': 'ok'})
    except Exception:
        return error_response('Database unavailable', 503)
```

#### 1b. Headless mode: hide desktop-only features

The containerised Photonarium runs headless -- there is no display server,
no tkinter folder picker, and no local file manager to "reveal" files in.
Rather than building fallback UIs (text-based path input, error toasts for
failed reveals), the Docker version should cleanly disable all desktop-only
features.

**Add a `headless` config option** (default `false`). The Docker entrypoint
sets it to `true`. When headless, `/api/config` returns `headless: true` and
the frontend hides:

- The entire **"Local Indexed Folders"** section (add folder, folder picker,
  rescan, remove buttons). In Docker, folders are pre-mounted as volumes and
  registered via `--add-folder` CLI flags in docker-compose.yml -- there is
  no need for runtime folder management in the UI.
- The **"Reveal in file manager"** button in the Gallery toolbar (calls
  `xdg-open`/`open`/`explorer` which don't exist in a container).
- The **native folder picker** path in the Import feature (if implemented).
  Import in Docker uses the file upload path only.

The backend `/api/pick-folder` and `/api/reveal` endpoints can remain but
will simply never be called when headless. No fallback text inputs needed.

**Files:** `app/config.py` (add `headless: bool = False` to Config),
`app/app.py` (include in `/api/config` response),
`docker/entrypoint.sh` (set `headless: true` in generated config),
`app/static/database.js` (check headless flag, hide folder section),
`app/static/gallery.js` (check headless flag, hide reveal button).

This is simpler than the previous text-input fallback approach and gives
Docker users a cleaner, less confusing UI.

#### 1c. Scheduled automatic rescans

Essential for NAS deployments. Many NAS users have apps that sync photos from
Apple Photos, Google Photos, Dropbox, OneDrive, etc. into local folders. These
folders grow constantly without Photonarium knowing about it. In the desktop
version, users can click "Rescan Local Folders" manually, but the Docker UI
hides that section (headless mode). Without scheduled rescans, synced photos
would sit unindexed indefinitely.

PhotoPrism solves this with `PHOTOPRISM_INDEX_SCHEDULE` (cron expression) and
`PHOTOPRISM_AUTO_INDEX` (delay in seconds after WebDAV upload).

**Add a `scan_schedule` config option** -- either a cron expression (flexible
but complex for non-technical users) or a simple interval in minutes (e.g.,
`scan_interval_minutes: 60`). The simpler interval approach is more in keeping
with Photonarium's non-technical ethos.

**Implementation:** A daemon thread (similar to the existing TrashWorker
pattern) that sleeps for the configured interval, then checks whether
it's safe to scan. The mtime-based fast path means only genuinely new or
changed files get processed, so scans are cheap when nothing has changed.
The thread integrates with the existing graceful shutdown mechanism.

**Deferral logic:** The timer does not blindly fire every N minutes. If any
background processing is still running when the interval elapses -- an
unfinished scan, a large import, face detection, embedding computation --
the scheduled scan waits until all processing completes, then restarts the
interval timer from that point. This prevents scans from piling up or
running back-to-back when photos are arriving quickly (e.g., a phone sync
uploading hundreds of photos over several minutes). The sequence is always:
all processing finishes -> wait full interval -> scan -> process -> wait
full interval -> and so on. A manual "Rescan" from the UI resets the timer
in the same way.

**Default:** Disabled (`0`) for desktop installs (where the user controls when
to scan). The Docker entrypoint config sets a sensible default like `60`
(rescan every hour).

**Files:** `app/config.py` (add `scan_interval_minutes: int = 0`),
`app/imagedb.py` (add scan timer thread, start in `start_threads()`),
`docker/entrypoint.sh` (set default in generated config).

**Status display:** When a scheduled scan is active, the existing processing
status display in the Database screen shows progress as normal. The frontend
doesn't need to know or care whether the scan was triggered manually or by
the timer.

### Phase 2: Docker Files

#### 2a. `Dockerfile`

```dockerfile
# --- Stage 1: Build dependencies ---
FROM python:3.12-slim AS base

# System dependencies for opencv-headless, Pillow, rawpy
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 libraw-dev gcc g++ \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements-docker.txt .

# CPU-only PyTorch (saves ~800MB vs CUDA)
RUN pip install --no-cache-dir \
    torch torchvision --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir -r requirements-docker.txt

# --- Stage 2: Application ---
FROM base AS app
WORKDIR /app
COPY . .

# Download ALL ML models into a staging directory inside the image.
# On first run, entrypoint.sh copies them to /config (the user's volume).
# This includes: OpenCLIP, BLIP, LAION aesthetic head, NIMA weights.
RUN HF_HUB_OFFLINE=0 python download_models.py --data-dir /defaults

# Entrypoint script handles PUID/PGID and first-run setup
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 5000
VOLUME ["/config", "/catalogue"]
HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 \
    CMD curl -f http://localhost:5000/api/health || exit 1

ENTRYPOINT ["/entrypoint.sh"]
```

Notes:
- `python:3.12-slim` is Debian-based (good NAS compatibility, multi-arch)
- `opencv-python-headless` in requirements (no Qt/GTK GUI deps)
- Multi-stage not strictly needed here but keeps the pattern extensible
- `--start-period=120s` gives time for model loading on first request
- `curl` is available in python:3.12-slim for healthcheck

#### 2b. `requirements-docker.txt`

A pinned requirements file for reproducible Docker builds. Generated from the
current install commands but using `opencv-python-headless` instead of
`opencv-python`:

```
open_clip_torch
pillow
opencv-python-headless
imagehash
numpy
pyyaml
flask
waitress
orjson
requests
transformers
rawpy
exifread
```

Plus `facenet-pytorch` installed separately with `--no-deps`.

Note: `torch` and `torchvision` are installed separately in the Dockerfile
with the CPU-only index URL, so they're NOT in this file.

#### 2c. `docker/entrypoint.sh`

```bash
#!/bin/bash
set -e

# --- PUID/PGID handling ---
PUID=${PUID:-1000}
PGID=${PGID:-1000}

# Create group and user if they don't exist
groupadd -o -g "$PGID" photonarium 2>/dev/null || true
useradd -o -u "$PUID" -g "$PGID" -d /config -s /bin/bash photonarium 2>/dev/null || true

# --- First-run setup ---
# Copy all baked-in models to /config if not already present.
# Uses cp -n (no-clobber) so user-replaced models aren't overwritten.
# Includes: HuggingFace models (OpenCLIP, BLIP), LAION head, NIMA weights.
if [ -d "/defaults" ]; then
    cp -rn /defaults/. /config/
fi

# Create default config if not present
if [ ! -f /config/photonarium.yml ]; then
    cat > /config/photonarium.yml << 'EOF'
# Photonarium configuration (Docker)
# See documentation for all available settings.
data_dir: /config
server_host: 0.0.0.0
server_port: 5000
headless: true
catalogue_dir: /catalogue
scan_interval_minutes: 60
EOF
fi

# Fix ownership
chown -R "$PUID:$PGID" /config

# --- Start application ---
exec gosu "$PUID:$PGID" python /app/app.py \
    --config /config/photonarium.yml \
    --data-dir /config \
    --port "${PHOTONARIUM_PORT:-5000}" \
    "$@"
```

Notes:
- `gosu` drops root privileges to the PUID/PGID user (standard NAS pattern)
- `exec` replaces the shell so the Python process is PID 1 (receives SIGTERM)
- `"$@"` passes any extra args (e.g. `--scan --detect-faces`)
- First-run creates a minimal config and copies all ML models to `/config`
- `/defaults/` inside the image contains everything `download_models.py`
  produces -- HuggingFace models, LAION head, NIMA weights
- At runtime, the app reads all models from `/config` (= `data_dir`), never
  from inside the image layer

Dependency: `gosu` needs to be installed in the Dockerfile:
```dockerfile
RUN apt-get update && apt-get install -y --no-install-recommends gosu ...
```

#### 2d. `docker-compose.yml`

```yaml
services:
  photonarium:
    container_name: photonarium
    image: photonarium/photonarium:${PHOTONARIUM_VERSION:-latest}
    restart: unless-stopped
    ports:
      - "${PHOTONARIUM_PORT:-5000}:5000"
    volumes:
      - ${CONFIG_PATH:-./config}:/config        # App data - MUST be local, not NFS/SMB
      - ${CATALOGUE_PATH}:/catalogue            # Imported photos (organised by date)
      - /etc/localtime:/etc/localtime:ro        # Timezone sync (EXIF timestamps)
      # Optional: mount existing photo libraries for indexing
      # - /nas/photos:/photos/main:ro
      # - /nas/holidays:/photos/holidays:ro
    env_file:
      - .env
    # Optional: register mounted photo folders on startup
    # command: --add-folder /photos/main --add-folder /photos/holidays --scan
```

#### 2e. `.env.example`

Ship alongside `docker-compose.yml` for easy configuration. Users copy to `.env`
and edit. This is the Docker convention and works well with NAS management UIs
(Portainer, etc.) that have `.env` file editors.

```
# Photonarium Docker Configuration
# Copy this file to .env and edit to match your setup.

# User/group ID - match your NAS user (run: id -u / id -g)
PUID=1000
PGID=1000

# Server port
PHOTONARIUM_PORT=5000

# Path to app data (DB, thumbnails, config) - MUST be local storage, not NFS
CONFIG_PATH=./config

# Path to catalogue directory for imported photos (can be on HDD/NAS share)
CATALOGUE_PATH=/path/to/catalogue

# Image version (default: latest)
# PHOTONARIUM_VERSION=latest
```

#### 2f. GPU acceleration compose files

Following Immich's pattern, GPU support uses separate compose files that extend
the base config via `docker compose -f docker-compose.yml -f <hwaccel>.yml up`.

**`hwaccel.cuda.yml`** (NVIDIA - Unraid, custom builds):

```yaml
services:
  photonarium:
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
    environment:
      - NVIDIA_VISIBLE_DEVICES=all
      - NVIDIA_DRIVER_CAPABILITIES=compute,utility
```

Requires the NVIDIA Container Toolkit on the host and a CUDA-enabled image.
The Dockerfile uses a build arg to select the PyTorch variant:

```dockerfile
ARG TORCH_INDEX=https://download.pytorch.org/whl/cpu
RUN pip install torch torchvision --index-url ${TORCH_INDEX}
```

CUDA builds are managed via Makefile targets:

```bash
make build-cu118    # CUDA 11.8 (GTX 10xx, RTX 20xx)
make build-cu126    # CUDA 12.6 (RTX 30xx, 40xx)
make build-cu128    # CUDA 12.8 (RTX 50xx / Blackwell) [speculative]
```

**`hwaccel.intel.yml`** (Intel iGPU - many Synology/QNAP NAS devices):

```yaml
services:
  photonarium:
    devices:
      - /dev/dri:/dev/dri
    group_add:
      - video
      - render
```

Intel iGPU acceleration is arguably more relevant for the NAS audience than
NVIDIA, since Celeron/Atom CPUs with integrated graphics are the most common
NAS processors. PyTorch supports Intel GPUs via the `intel-extension-for-pytorch`
package, or alternatively OpenVINO can be used for inference. This needs
investigation during implementation to determine the best approach for
Photonarium's workloads (CLIP, MTCNN, InceptionResnet, NIMA).

**Note:** Immich also supports ROCm (AMD) and ARM NN (Mali), but these are
uncommon on NAS hardware and can be added later if there is demand.

#### 2g. `Makefile`

GNU Make orchestrates all Docker builds. This provides a clean interface for
building multiple image variants without remembering build-arg syntax:

```makefile
# PyTorch index URLs
TORCH_CPU   := https://download.pytorch.org/whl/cpu
TORCH_CU118 := https://download.pytorch.org/whl/cu118
TORCH_CU126 := https://download.pytorch.org/whl/cu126
TORCH_CU128 := https://download.pytorch.org/whl/cu128

.PHONY: build build-cu118 build-cu126 build-cu128 build-intel all-images \
        test up down logs shell clean help

build:           ## Build CPU-only image (default, ~3.5 GB)
	docker build --build-arg TORCH_INDEX=$(TORCH_CPU) \
		-t photonarium:latest -t photonarium:cpu -f docker/Dockerfile .

build-cu118:     ## Build CUDA 11.8 image (GTX 10xx, RTX 20xx)
build-cu126:     ## Build CUDA 12.6 image (RTX 30xx, 40xx)
build-cu128:     ## Build CUDA 12.8 image (RTX 50xx / Blackwell) [speculative]
build-intel:     ## Build Intel iGPU image (IPEX)

all-images:      ## Build all image variants

test:            ## Run container smoke test (health check)
up:              ## Start container (docker compose up -d)
down:            ## Stop container (docker compose down)
logs:            ## Follow container logs
shell:           ## Open shell in running container

clean:           ## Remove all built images
help:            ## Show available targets (default)
```

The Makefile lives in the repository root (not inside `docker/`) so users can
run `make build` from the top level. All Docker-related files are referenced
via `docker/` paths (e.g., `-f docker/Dockerfile`).

#### 2h. `.dockerignore`

```
env/
.git/
__pycache__/
*.pyc
.photonarium.yml
photonarium.db
.thumbnails/
trash/
demo-seed/
www/
node_modules/
.eslintcache
```

### Phase 3: Documentation

#### 3a. Add Docker section to `README.md`

Add a "Docker / NAS" section covering:
- Quick start with `docker compose up`
- Volume explanations (what goes where, local-only warning for `/config`)
- PUID/PGID for NAS permissions
- Adding photo folders (CLI and web UI)
- GPU variants (NVIDIA CUDA, Intel iGPU)
- Reverse proxy guidance (see below)
- Platform-specific notes (Synology, Unraid, QNAP, TrueNAS, Proxmox)

#### 3b. Update `DEVELOP.md`

Add Docker files to the file listing. Document the build process and
architecture decisions.

#### 3c. Update `CLAUDE.md`

Add Docker build/run commands to the CLI section.

#### 3d. Reverse proxy guidance

NAS users commonly run multiple services behind a reverse proxy. Document
configurations for nginx, Caddy, and Traefik. Key considerations:

**nginx:**
```nginx
server {
    listen 80;
    server_name photos.example.com;

    # Large limit needed for the Import feature (file uploads)
    client_max_body_size 5000M;

    # Increased timeouts for long-running ML operations
    proxy_read_timeout 600s;
    proxy_send_timeout 600s;

    location / {
        proxy_pass http://localhost:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

**Caddy** (simplest - auto-HTTPS):
```
photos.example.com {
    reverse_proxy localhost:5000
}
```

**Traefik:**
- Increase `respondingTimeouts` on the entrypoint to 600s (default 60s will
  kill long-running requests like batch import or ML processing).
- Standard Docker labels on the photonarium service for routing.

**Notes:**
- Photonarium can be served at a domain root or behind a sub-path (Flask
  handles relative URLs). Document both patterns. This is an advantage over
  PhotoPrism and Immich, which both require serving at the domain root.
- Upload size limit matters for the Import feature's file upload path. Without
  it, large photo uploads via mobile/remote will fail at the proxy level.

#### 3e. Backup guidance

PhotoPrism has built-in automated daily DB backups with configurable retention.
Photonarium should document backup procedures even if automated backup is
deferred:

**What to back up:**
1. `/config/photonarium.db` -- The SQLite database (critical)
2. `/config/photonarium.yml` -- Configuration file
3. `/photos` -- Your actual photos (you probably already back these up)

**What can be regenerated (skip to save space):**
- `/config/.thumbnails/` -- Regenerated by `--generate-thumbnails`
- `/config/.cache/huggingface/` -- Copied from image on first run (~2.5 GB)
- `/config/.laion-aesthetic-head.pth`, `.nima-mobilenetv2-ava.pth` -- Copied
  from image on first run

**Simple backup (while container is running):**
```bash
# SQLite supports online backup via .backup command
docker exec photonarium sqlite3 /config/photonarium.db ".backup /config/photonarium-backup.db"
# Then copy from the host:
cp ./config/photonarium-backup.db /path/to/backup/
cp ./config/photonarium.yml /path/to/backup/
```

**Note:** Do NOT simply copy `photonarium.db` while the container is running --
SQLite WAL mode means the `-wal` and `-shm` files must be consistent. Use the
`.backup` command or stop the container first.

**Future enhancement:** Consider adding a `PHOTONARIUM_BACKUP_SCHEDULE` config
option (cron format) for automated DB backups, following PhotoPrism's pattern.
This would be a small addition to the backend (run `sqlite3 .backup` on a
timer thread) with high value for NAS users.

#### 3f. Platform-specific deployment notes

**Synology:**
- Deploy via Docker and Portainer (Container Manager).
- Mount `/config` on the SSD cache volume if space permits (15-20 GB+ for a
  medium library), otherwise the main storage pool works with slightly slower
  performance. `/photos` on the main storage pool.
- PUID/PGID should match the Synology user that owns the photo share.

**Unraid:**
- Install via Community Apps template or Docker Compose (Compose Manager plugin).
- `CONFIG_PATH` on the cache drive (appdata share) if space allows, otherwise
  any share. Expect 15-20 GB+ for a medium-sized library.
- Photo and catalogue volumes can be any Unraid share.
- For GPU acceleration, Unraid does not support multiple compose files natively.
  The hardware acceleration config must be inlined into the main compose file.

**TrueNAS:**
- Deploy via the TrueNAS Apps system (community train) or Docker Compose.
- Create datasets for config, catalogue, and photos.
- `/config` on SSD pool if space allows (15-20 GB+), otherwise HDD pool is
  fine with slightly slower performance.

**QNAP:**
- Deploy via Container Station using docker-compose files.
- Similar configuration to standard Docker deployment.

**Proxmox:**

Proxmox users have three deployment options:

- **VM + Docker (safe path):** Create a standard Linux VM (Ubuntu/Debian),
  install Docker Engine inside, and deploy with docker-compose. GPU passthrough
  via Proxmox PCI passthrough (requires IOMMU/VT-d). Most reliable for NVIDIA
  GPUs. Higher resource overhead than LXC.

- **LXC + Docker (lighter weight):** Docker inside LXC is fragile and may
  break on Proxmox upgrades. However, Photonarium's single-container nature
  makes it a better LXC candidate than multi-container apps. The PhotoPrism
  community commonly uses LXC on Proxmox (automated scripts like tteck's
  helper scripts exist), and their experience shows it works well for single-
  container apps when `nesting=1` is enabled. Intel iGPU passthrough to LXC
  is straightforward (mount `/dev/dri`). NVIDIA in LXC is more complex --
  consider a VM instead.

- **LXC native (no Docker, recommended for Proxmox):** Skip Docker entirely
  and install Photonarium directly in a Debian LXC container. Install Python
  deps, clone the repo, run `app.py`. This avoids all Docker-in-LXC
  compatibility issues and is the lightest option. Minimum spec: 2 cores,
  3 GB RAM, 16 GB root storage. PhotoPrism's community project
  `immich-in-lxc` proves this pattern works well for photo management apps.
  Photonarium's single-process design makes it an even simpler candidate.

  Mount NAS storage from Proxmox into the LXC or VM via NFS/SMB mount points
  (add mount points in the container config, e.g.,
  `mp0: /mnt/nas/photos,mp=/photos`).

---

## What This Plan Does NOT Include

- **Multi-arch builds** (arm64 for ARM-based NAS like some Synology models).
  This is important but adds CI/CD complexity. Can be added later with
  `docker buildx` once the x86_64 image is stable. The `python:3.12-slim`
  base image already supports arm64, so the main blocker is PyTorch arm64
  wheels and CI/CD setup, not fundamental architecture issues.
- **Remote ML offloading.** Immich's most popular NAS feature is running the
  ML container on a separate GPU-equipped machine while the server runs on
  the NAS. Photonarium's architecture has ML in-process (not a separate
  service), so splitting it out would be a major refactor. Not for MVP, but
  worth considering as a future enhancement for users whose NAS has no GPU.
  For now, the CPU-only approach with optional CUDA/Intel iGPU is the path.
- **Scheduled automatic rescans.** Moved to Phase 1 -- see task 1c below.
- **Docker Hub / GHCR publishing.** The image can be built locally. Publishing
  to a registry is a distribution concern, not an implementation concern.
- **Watchtower / auto-update labels.** Nice-to-have, not MVP.
- **Kubernetes / Helm charts.** Overkill for the target audience.

---

## Implementation Order

1. **`/api/health` endpoint** (5 min, tiny change to app.py)
2. **`headless` config option** (30 min, config.py + app.py + database.js + gallery.js)
3. **Scheduled automatic rescans** (1 hour, config.py + imagedb.py)
4. **`Makefile`** (15 min, new file, orchestrates all builds)
5. **`docker/Dockerfile`** (30 min, new file, iterative testing)
6. **`docker/requirements.txt`** (10 min, new file)
7. **`docker/entrypoint.sh`** (20 min, new file)
8. **`docker/docker-compose.yml`** + `docker/.env.example` (15 min, new files)
9. **`docker/hwaccel.cuda.yml`** + **`docker/hwaccel.intel.yml`** (15 min, new files)
10. **`.dockerignore`** (5 min, new file)
11. **Build and test** (1-2 hours, iterative via `make build`, `make test`)
12. **Documentation** (1 hour, README + DEVELOP + CLAUDE + reverse proxy + platform notes)

Steps 1-3 are backend/frontend changes that benefit all users (not just
Docker). Steps 4-10 are Docker-only files in the `docker/` subdirectory (except
the root Makefile and .dockerignore). Step 11 requires a Linux environment (or
WSL2) to test the actual container build and run cycle.

---

## Open Questions

1. **Photo library read-only?** Should `/photos` be mounted read-only by
   default? Trash moves files and rotate rewrites JPEGs, both requiring write
   access. PhotoPrism offers an explicit `PHOTOPRISM_READONLY` env var for
   this. Decision: recommend `:ro` mounts for existing libraries by default
   (safer -- prevents accidental modification of originals). Trash and rotate
   would only apply to photos in the catalogue. Consider adding a
   `PHOTONARIUM_READONLY` env var that disables trash/rotate in the UI for
   photos on read-only mounts (cleaner than silent OS permission errors).

2. **Multi-folder mounts?** A user might have photos on multiple NAS shares.
   Docker compose supports multiple volume mounts. The user would mount them
   at `/photos/share1`, `/photos/share2`, etc. and register each via
   `--add-folder`. This works with the existing design - no code changes
   needed.

3. **Config file generation.** Should `download_models.py` or a new setup
   script generate the Docker config? Or is the entrypoint's default config
   sufficient? Decision: entrypoint generates a minimal default. Users edit
   `/config/photonarium.yml` for customisation.

4. **Image size budget.** With CPU-only PyTorch (~800MB), OpenCLIP (~350MB),
   BLIP-large (~2GB), Python base (~150MB), and app deps (~200MB), the image
   will be ~3.5GB. This is comparable to PhotoPrism (~3GB) and much smaller
   than Immich (multiple containers totalling ~5GB+). Acceptable for NAS
   deployment. Note that the models are also copied to `/config` on first
   run, so the total disk footprint is image (~3.5GB) + `/config` models
   (~5-10GB) + database + thumbnails. For a medium-sized library (65k
   images), expect `/config` to reach 15-20 GB.

5. **Intel iGPU acceleration approach.** PyTorch has two paths for Intel GPU:
   `intel-extension-for-pytorch` (IPEX) and OpenVINO (ONNX-based inference).
   IPEX is closer to native PyTorch but adds ~1GB to image size. OpenVINO
   requires model conversion but is more mature for inference-only workloads.
   Need to investigate which works best for Photonarium's models (OpenCLIP
   ViT-B-32, MTCNN, InceptionResnetV1, MobileNetV2-NIMA, BLIP) and whether
   the performance gain on a Celeron N5105/N6005 justifies the complexity.
   Decision deferred to implementation.

6. **SQLite on network storage.** The plan correctly requires `/config` on
   local storage. However, some NAS setups (e.g., Proxmox LXC with NFS-
   mounted storage) might accidentally put everything on network storage.
   The entrypoint script could add a runtime check (attempt an exclusive
   lock on the database file) and warn loudly if it appears to be on a
   network filesystem. Decision: add a warning log message on startup if
   the config directory is on NFS/SMB (detect via `stat -f` filesystem
   type or `df` output), but don't block startup.

7. **Swap requirements.** PhotoPrism recommends at least 4 GB swap to prevent
   OOM kills during indexing of large files (high-res panoramas, RAW images).
   The entrypoint script could check available swap and warn if insufficient,
   since NAS devices often have minimal swap configured. Alternatively,
   document recommended swap sizes and how to add swap on each NAS platform.

8. **Import volume.** Resolved: the catalogue gets its own `/catalogue`
   volume mount, separate from `/config` (SSD, small) and `/photos` (existing
   libraries). This follows PhotoPrism's 3-volume pattern (storage, originals,
   import) and allows the catalogue to live on a large HDD/NAS share while
   `/config` stays on fast local SSD. The entrypoint sets `catalogue_dir:
   /catalogue` in the default config.
