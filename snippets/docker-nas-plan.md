# Docker / NAS Deployment Plan

Implementation plan for running Photonarium as a Docker container on NAS
devices (Synology, QNAP, Unraid, TrueNAS) and generic Linux servers.

---

## Design Decisions

### Single-container architecture

Photonarium is a single-process Python app with SQLite (no Postgres, Redis, or
worker queues). This maps naturally to a single Docker container, following the
LinuxServer.io pattern rather than the Immich/PhotoPrism multi-container
pattern. A single container is simpler to deploy, back up, and reason about -
important for home NAS users who aren't Docker experts.

### Models baked into the image

HuggingFace models (OpenCLIP ~350MB, BLIP ~2GB) live in `~/.cache/huggingface/`
which is outside `data_dir`. Two options exist:

- **Option A: Bake into image** - Run `download_models.py` during `docker build`.
  Image is ~3.5GB but "just works" with no internet on first run.
- **Option B: Volume-mount cache** - Smaller image, but requires the user to
  either run a download step or have internet on first start.

**Decision: Option A (bake in).** The target audience (NAS users) values
simplicity over image size. PhotoPrism uses the same approach. The LAION and
NIMA weights (~11KB + ~9MB) go into `/config` at first run since they live in
`data_dir`.

### CPU-only base image, GPU optional

Most NAS devices have no GPU. The default image uses CPU-only PyTorch (~800MB
smaller than CUDA). A separate `Dockerfile.cuda` (or build arg) can produce a
GPU-enabled variant for power users with NVIDIA GPUs on Unraid/QNAP.

### PUID/PGID for NAS filesystem permissions

NAS platforms mount photo libraries with specific user/group ownership. The
container must run as the host user's UID/GID to avoid permission errors on
mounted volumes. This is the universal NAS convention (LinuxServer.io, Immich,
PhotoPrism all do it).

---

## Volume Layout

```
/config     Persistent application data (database, thumbnails, trash, config)
            Maps to: e.g. /volume1/docker/photonarium on Synology
/photos     Read-only or read-write photo library
            Maps to: e.g. /volume1/photos on Synology
```

Inside `/config`:
```
/config/photonarium.yml          Configuration file
/config/photonarium.db           SQLite database
/config/.thumbnails/             Thumbnail cache (200px + 400px)
/config/trash/                   Trashed images (unless overridden)
/config/.laion-aesthetic-head.pth   LAION aesthetic weights (downloaded on first run)
/config/.nima-mobilenetv2-ava.pth   NIMA aesthetic weights (downloaded on first run)
```

The `/photos` mount is the user's existing photo library. Photonarium indexes
it but does not modify originals (only trash moves files out). If the user
wants trash to go elsewhere, they can set `trash_dir` in the config.

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

#### 1b. Add text-based folder input to the frontend

The native folder picker (`tkinter.filedialog`) doesn't work in headless
Docker containers. NAS users need a way to type/paste folder paths in the
web UI.

**Files:** `static/index.html`, `static/database.js`

**HTML change** (`index.html`): Add a text input + button next to the existing
"Add Folder" button inside the `.folder-controls` div:
```html
<div class="folder-path-input" id="folder-path-input" style="display:none">
    <input type="text" id="folder-path-text" placeholder="/path/to/photos"
           title="Type or paste the full path to a photo folder">
    <button id="btn-add-path" class="action-btn" title="Add this folder path">
        <span class="icon" data-icon="add">+</span>
    </button>
    <button id="btn-cancel-path" class="action-btn" title="Cancel">
        <span class="icon" data-icon="close">X</span>
    </button>
</div>
```

**JS change** (`database.js`): The existing `_addFolder()` method calls
`_pickFolder()` which calls `/api/pick-folder`. Two approaches:

- **Approach A (auto-detect):** Try the native picker first. If it returns
  null (headless), show the text input as fallback. This gives desktop users
  the native experience and Docker users a working alternative.
- **Approach B (config-driven):** Backend returns a `headless` flag from
  `/api/config`. Frontend shows the text input instead of the native picker
  button when headless.

**Decision: Approach A (auto-detect).** No config needed. The native picker
already returns null on failure, so the fallback is a natural extension.

Flow: Click "Add Folder" -> try native picker -> if null, show text input
inline -> user types path -> validate via existing `POST /api/folders` ->
hide input.

**CSS change** (`styles.css`): Style the inline path input to match the
existing folder controls aesthetic.

#### 1c. Hide "Reveal" buttons when headless

The "Open containing folder" button in the Gallery toolbar calls `/api/reveal`
which requires a desktop display server. In Docker it silently fails.

**Option A:** Backend returns a `desktop_features` flag in `/api/config`,
frontend hides the reveal button when false.
**Option B:** The reveal button already does nothing on failure (no crash, no
error toast). Just leave it.

**Decision: Option A.** A button that does nothing is confusing. Add a simple
boolean to the config response. Detect headless by checking if `DISPLAY` env
var is set (Linux) or if the platform is in a known container environment.
Actually, simpler: add a `headless` config option (default false). Users set it
to true in their Docker config, or the entrypoint script sets it. Frontend
hides reveal + native picker when true.

Actually, even simpler: try the reveal, and if subprocess fails, return an
error response that the frontend can use to show a toast. Currently the reveal
endpoint has no error feedback for "no display server". This avoids any config
flag - it just works (or tells you it didn't).

**Revised decision:** Enhance `/api/reveal` to catch `FileNotFoundError` (no
`xdg-open`) and `subprocess` errors, returning a clear error message. Frontend
shows a toast. No config flag needed. The button stays visible but gives
feedback instead of silently failing.

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

# Download ML models into the image layer
# Models go to /root/.cache/huggingface/ (baked into image)
# LAION/NIMA go to /defaults/ (copied to /config on first run)
RUN python download_models.py --data-dir /defaults

# Entrypoint script handles PUID/PGID and first-run setup
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 5000
VOLUME ["/config", "/photos"]
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
# Copy default LAION/NIMA weights to /config if not present
for f in .laion-aesthetic-head.pth .nima-mobilenetv2-ava.pth; do
    if [ -f "/defaults/$f" ] && [ ! -f "/config/$f" ]; then
        cp "/defaults/$f" "/config/$f"
    fi
done

# Create default config if not present
if [ ! -f /config/photonarium.yml ]; then
    cat > /config/photonarium.yml << 'EOF'
# Photonarium configuration (Docker)
# See documentation for all available settings.
data_dir: /config
server_host: 0.0.0.0
server_port: 5000
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
- First-run creates a minimal config pointing at `/config` as data dir
- `/defaults/` contains the LAION/NIMA weights from the build stage
- HuggingFace cache stays at `/root/.cache/huggingface/` (baked into image)

Dependency: `gosu` needs to be installed in the Dockerfile:
```dockerfile
RUN apt-get update && apt-get install -y --no-install-recommends gosu ...
```

#### 2d. `docker-compose.yml`

```yaml
services:
  photonarium:
    image: photonarium/photonarium:latest
    container_name: photonarium
    restart: unless-stopped
    ports:
      - "5000:5000"
    volumes:
      - ./config:/config          # App data (DB, thumbnails, config)
      - /path/to/photos:/photos   # Your photo library
    environment:
      - PUID=1000                 # Your user ID (run: id -u)
      - PGID=1000                 # Your group ID (run: id -g)
    # Optional: Add more photo folders
    # command: --add-folder /photos/holidays --add-folder /photos/family --scan
```

#### 2e. `docker-compose.gpu.yml` (GPU override)

```yaml
services:
  photonarium:
    # Use with: docker compose -f docker-compose.yml -f docker-compose.gpu.yml up
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

This requires a separate `Dockerfile.cuda` that installs CUDA-enabled PyTorch
instead of CPU-only. Or a build arg:
```dockerfile
ARG TORCH_INDEX=https://download.pytorch.org/whl/cpu
RUN pip install torch torchvision --index-url ${TORCH_INDEX}
```

Build with: `docker build --build-arg TORCH_INDEX=https://download.pytorch.org/whl/cu124 -t photonarium:cuda .`

#### 2f. `.dockerignore`

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
- Volume explanations (what goes where)
- PUID/PGID for NAS permissions
- Adding photo folders (CLI and web UI)
- GPU variant (optional)
- Platform-specific notes (Synology, Unraid, QNAP)

#### 3b. Update `DEVELOP.md`

Add Docker files to the file listing. Document the build process and
architecture decisions.

#### 3c. Update `CLAUDE.md`

Add Docker build/run commands to the CLI section.

---

## What This Plan Does NOT Include

- **Multi-arch builds** (arm64 for ARM-based NAS like some Synology models).
  This is important but adds CI/CD complexity. Can be added later with
  `docker buildx` once the x86_64 image is stable.
- **Automatic folder scanning on mount.** Users must register folders via the
  web UI or `--add-folder` CLI flag, then scan with `--scan` or the rescan
  button. This is the existing design and works fine for Docker.
- **Docker Hub / GHCR publishing.** The image can be built locally. Publishing
  to a registry is a distribution concern, not an implementation concern.
- **Watchtower / auto-update labels.** Nice-to-have, not MVP.
- **Kubernetes / Helm charts.** Overkill for the target audience.

---

## Implementation Order

1. **`/api/health` endpoint** (5 min, tiny change to app.py)
2. **Improve `/api/reveal` error handling** (10 min, app.py)
3. **Text-based folder input** (1-2 hours, index.html + database.js + styles.css)
4. **`requirements-docker.txt`** (10 min, new file)
5. **`.dockerignore`** (5 min, new file)
6. **`Dockerfile`** (30 min, new file, iterative testing)
7. **`docker/entrypoint.sh`** (20 min, new file)
8. **`docker-compose.yml`** + GPU variant (15 min, new files)
9. **Build and test** (1-2 hours, iterative)
10. **Documentation** (30 min, README + DEVELOP + CLAUDE updates)

Steps 1-3 are backend/frontend changes that benefit all users (not just
Docker). Steps 4-8 are Docker-only files. Step 9 requires a Linux environment
(or WSL2) to test the actual container build and run cycle.

---

## Open Questions

1. **Photo library read-only?** Should `/photos` be mounted read-only by
   default? Trash moves files, which requires write access to the source.
   If read-only, trash would need to copy instead of move (slower, uses more
   space). Decision: mount read-write by default, document read-only as an
   option with the caveat that trash won't work.

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
   deployment.
