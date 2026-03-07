# Installation

[< Back to README](../README.md)

Photonarium can be installed via Docker (easiest, especially for NAS devices) or directly on your system. Choose whichever suits your setup.

- [Docker Installation](#docker-installation) -- recommended for NAS, servers, and containerised deployments
- [Direct Installation](#direct-installation) -- for running natively on your desktop or laptop

---

# Docker Installation

Docker is the easiest way to run Photonarium, especially on NAS devices (Synology, QNAP, Unraid, etc.) or any system where you want a self-contained deployment. All ML models are pre-downloaded in the image, so you can start using Photonarium immediately.

## Quick Start

Pull and run the CPU image (works on any system):

```bash
# Create directories for persistent data
mkdir -p ~/photonarium/config ~/photonarium/catalogue

# Run the container
docker run -d \
  --name photonarium \
  -p 5000:5000 \
  -v ~/photonarium/config:/config \
  -v ~/photonarium/catalogue:/catalogue \
  -v /path/to/your/photos:/photos:ro \
  -e PUID=$(id -u) \
  -e PGID=$(id -g) \
  7thsw/photonarium:latest \
  --add-folder /photos --scan --detect-faces
```

Then open `http://localhost:5000` in your browser. Your photos and videos will start indexing automatically.

The `--add-folder /photos` flag registers the mounted directory (only needed on first run -- folders are saved in the database). The `--scan` flag triggers indexing. The `--add-folder` flag is needed because Docker runs in headless mode, which hides the GUI "Add Folder" button (native folder picker dialogs don't work without a display). The folder list and Rescan button remain available in the web UI. The `--detect-faces` flag causes face detection to run on images as they are indexed after startup.

On subsequent runs, you can omit `--add-folder` and just use `--scan --detect-faces` to pick up new files, or omit these flags entirely and use the **Rescan Local Folders** button in the web UI.

## Image Variants

Pre-built images are available on DockerHub at `7thsw/photonarium`:

| Tag | Size | Best For |
|-----|------|----------|
| `latest` / `cpu` | ~4.5 GB | Most NAS devices, systems without a dedicated GPU |
| `cu118` | ~8 GB | NVIDIA GTX 10-series, RTX 20-series (CUDA 11.8) |
| `cu126` | ~10 GB | NVIDIA RTX 30-series, 40-series (CUDA 12.6) |
| `cu128` | ~10 GB | NVIDIA RTX 50-series / Blackwell (CUDA 12.8) |
| `intel` | ~5 GB | Intel integrated graphics (Celeron/Atom NAS CPUs) |
| `arm64` | ~4 GB | ARM64 systems (Raspberry Pi 4/5, Apple Silicon) |

The CPU and CUDA images are x86_64 only. Use the `arm64` tag for ARM-based systems. The CPU/arm64 images work without a dedicated GPU but process images and videos more slowly. If you have a supported GPU, use the matching CUDA or Intel variant for significantly faster indexing and face detection.

## Using Docker Compose

Docker Compose makes it easier to manage configuration. Create a `docker-compose.yml` file:

```yaml
services:
  photonarium:
    container_name: photonarium
    image: 7thsw/photonarium:latest
    restart: unless-stopped
    ports:
      - "5000:5000"
    volumes:
      # Application data (database, thumbnails, models)
      - ./config:/config
      # Catalogue for imported photos and videos
      - ./catalogue:/catalogue
      # Your media library (read-only recommended)
      - /path/to/your/photos:/photos:ro
      # Timezone sync
      - /etc/localtime:/etc/localtime:ro
    environment:
      - PUID=1000
      - PGID=1000
    # Register folder and start indexing
    command: --add-folder /photos --scan --detect-faces
```

Then run:

```bash
docker compose up -d
```

The `command:` line registers your folder and starts processing. The `--add-folder` flag is idempotent (safe to repeat), so leaving it in the compose file is fine -- it won't create duplicates. On subsequent container restarts, registered folders are rescanned for new files.

### Multiple Folders

Mount each folder separately and register them all in the command:

```yaml
volumes:
  - ./config:/config
  - ./catalogue:/catalogue
  - /nas/photos/holidays:/photos/holidays:ro
  - /nas/photos/family:/photos/family:ro
  - /nas/photos/archive:/photos/archive:ro
command: >-
  --add-folder /photos/holidays
  --add-folder /photos/family
  --add-folder /photos/archive
  --scan --detect-faces
```

Each `--add-folder` flag registers a folder for indexing. Folders are stored in the database, so subsequent restarts will rescan them even if you remove the `--add-folder` flags from the command.

### Syncing Photos to Your NAS

Photonarium doesn't include built-in phone backup -- and that's intentional. NAS vendors and cloud services already have excellent sync tools, and there's no need to reinvent the wheel:

- **Synology**: Use [Cloud Sync](https://www.synology.com/en-us/dsm/feature/cloud_sync) to sync from Google Drive, Dropbox, OneDrive, etc., or [Synology Photos](https://www.synology.com/en-us/dsm/feature/photos) mobile app for phone backup
- **QNAP**: Use [HybridMount](https://www.qnap.com/en/software/hybrid-mount) or [Qsync](https://www.qnap.com/en/software/qsync) for phone backup
- **Unraid/TrueNAS**: Mount cloud storage via rclone, or use Nextcloud for phone backup
- **Any NAS**: Native mobile apps from Apple Photos, Google Photos, OneDrive, and Dropbox can back up to their respective clouds, which you then sync to your NAS

Once photos and videos land on your NAS (however they get there), mount that folder into Photonarium and it will index them. Your existing backup workflow stays unchanged -- Photonarium just adds AI-powered search and organisation on top.

## Hardware Acceleration

### NVIDIA GPUs

GPU acceleration dramatically speeds up image indexing, video processing, and face detection. To enable it:

1. **Install the NVIDIA Container Toolkit** on your host system. Follow the [official installation guide](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html).

2. **Use a CUDA-enabled image** that matches your GPU:
   - RTX 30-series, 40-series: `7thsw/photonarium:cu126`
   - RTX 20-series, GTX 10-series: `7thsw/photonarium:cu118`
   - RTX 50-series (Blackwell): `7thsw/photonarium:cu128`

3. **Add GPU access to your container**:

```yaml
services:
  photonarium:
    image: 7thsw/photonarium:cu126
    # ... other settings ...
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

To verify GPU access, check the container logs on startup -- it should show your GPU device.

### Intel Integrated Graphics

Many NAS devices (Synology, QNAP) have Intel Celeron or Atom CPUs with integrated graphics. The Intel image uses IPEX (Intel Extension for PyTorch) to accelerate computation on these iGPUs.

```yaml
services:
  photonarium:
    image: 7thsw/photonarium:intel
    # ... other settings ...
    devices:
      - /dev/dri:/dev/dri
    group_add:
      - video
      - render
```

Requires `/dev/dri` to be accessible on the host (standard on most Linux systems).

## Docker Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PUID` | 1000 | User ID for file ownership (match your NAS user) |
| `PGID` | 1000 | Group ID for file ownership |
| `PHOTONARIUM_PORT` | 5000 | Port the server listens on |

**Tip:** On Synology/QNAP, find your user's PUID/PGID with `id your_username` via SSH.

### Volumes

| Container Path | Purpose |
|----------------|---------|
| `/config` | Database, thumbnails, configuration file |
| `/catalogue` | Imported photos and videos (organised by date) |
| `/photos` | Your media library (mount read-only with `:ro`) |

### Configuration File

On first run, Photonarium creates `/config/photonarium.yml` with Docker-appropriate defaults:

- `headless: true` -- hides desktop-only features (folder picker dialogs, reveal in explorer)
- `scan_interval_minutes: 60` -- automatic rescan every hour (useful if files sync continuously)

Edit this file to change settings. Most settings can also be changed via the **Edit Settings** button in the web UI.

## Running on Proxmox (LXC Containers)

If you're running Docker inside a Proxmox LXC container, photo directories from the host must be bind-mounted into the LXC before Docker can see them. Docker bind mounts only work on paths that already exist inside the LXC.

**On the Proxmox host** (not inside the LXC), edit the container config:

```bash
# Replace 102 with your LXC container ID
nano /etc/pve/lxc/102.conf

# Add a bind mount for your photo directory:
mp0: /path/to/photos/on/proxmox,mp=/mnt/photos,ro=1
```

Then restart the LXC container. After this, `/mnt/photos` inside the LXC will have your files, and you can use it as a Docker volume:

```yaml
volumes:
  - /mnt/photos:/photos:ro
```

**Disk space:** The LXC needs enough storage for the Docker image (~4.5GB for CPU) plus the `/config` volume (database, thumbnails). For a small library, 15-20GB is sufficient. Larger libraries need more space for thumbnails.

## Performance Tips

### Put the Database on an SSD

The `/config` volume contains Photonarium's SQLite database and thumbnail cache. For best performance, especially with large libraries (50,000+ images):

- **Store `/config` on local SSD storage**, not network storage (NFS/SMB)
- SQLite requires a local filesystem with proper locking -- network storage causes corruption
- Thumbnails also benefit from fast random-read performance

Your media (`/photos`) can remain on slower network or HDD storage since files are read sequentially during scanning.

### Memory Considerations

- The CPU image uses ~2-3 GB RAM during normal operation (the ML models account for most of this)
- CUDA images may use more during batch processing
- Face detection, video processing, and image captioning temporarily spike memory usage
- For systems with limited RAM (e.g. NAS devices, small VMs), reduce `embedding_batch_size`, `face_detection_batch_size`, and `nima_batch_size` in settings (default: 16-32). Smaller batches use less memory at the cost of slower processing.
- The thumbnail RAM cache is configurable via `thumbnail_cache_size_mb` (default: 100MB). Reduce this on memory-constrained systems.
- **Graceful OOM handling:** If memory runs low during model loading or batch inference, Photonarium catches the error, logs a clear message, and either retries with a smaller batch or disables the affected feature -- rather than crashing the processing thread. On very constrained systems (e.g. Proxmox LXC with limited RAM), some features may be automatically disabled if there isn't enough memory to load their ML model.

### Network Storage for Media

Accessing photos and videos over NFS or SMB is fine for the `/photos` mount:

- Initial indexing may be slower due to network latency
- Thumbnail generation reads each file once, then serves from the local cache
- Subsequent browsing is fast because thumbnails are stored locally in `/config`

## Scheduled Rescans

For NAS setups where files are synced continuously (e.g., via cloud services), enable automatic periodic rescans by editing `/config/photonarium.yml`:

```yaml
# Rescan all folders every 60 minutes
scan_interval_minutes: 60
```

This runs in the background without blocking the UI. Combined with the `--scan` startup flag, this ensures new files are indexed automatically whether they arrive while the container is running or while it was stopped.

## Updating

To update to a new version:

```bash
# Pull the latest image
docker pull 7thsw/photonarium:latest

# Restart the container
docker compose down
docker compose up -d
```

Your data in `/config` and `/catalogue` is preserved across updates. ML models are baked into the image, so updates include the latest models automatically.

### Automatic update notifications

Docker does not notify you when a new image is available. Most NAS platforms handle this natively:

- **Synology** (Container Manager), **QNAP** (Container Station), and **Unraid** (Community Apps) show update badges and let you pull new images with a click.
- **TrueNAS SCALE** and **OpenMediaVault** don't have built-in container update notifications.

For bare Linux or Docker Compose setups, [Watchtower](https://containrrr.dev/watchtower/) can monitor running containers and automatically pull updated images:

```bash
# Run Watchtower alongside your containers -- it checks for updates daily
docker run -d --name watchtower \
    -v /var/run/docker.sock:/var/run/docker.sock \
    containrrr/watchtower
```

Alternatively, [Diun](https://crazymax.dev/diun/) sends notifications (email, Discord, Slack, etc.) without auto-updating, if you prefer to pull manually.

## Building from Source

If you want to build the image yourself (developers, custom modifications):

```bash
# Clone the repository
git clone https://github.com/7thsw/photonarium.git
cd photonarium

# Download ML models (run once, requires ~2.5GB disk space)
# This pre-downloads models so they can be baked into the image
make download-models

# Build CPU image (x86_64)
make build

# Build CUDA 12.6 image (x86_64, RTX 30xx/40xx)
make build-cu126

# Build ARM64 image (Raspberry Pi, Apple Silicon)
make build-arm64

# Build all variants
make all-images
```

The `make download-models` step downloads all ML models (OpenCLIP, BLIP, FaceNet, LAION, NIMA) to `docker/models/` so they can be copied into the Docker image during build. This only needs to be run once -- subsequent builds reuse the cached models. The build will fail with an error if models haven't been downloaded.

See the Makefile for all available build targets. Note that building ARM64 images on x86_64 uses QEMU emulation and is slow.

**Note:** `CLAUDE.md` (project context for [Claude Code](https://claude.ai/code)) is deliberately gitignored and is not part of the distributed source. Each developer maintains their own local copy.

---

# Direct Installation

If you prefer to run Photonarium directly on your system without Docker, follow these instructions.

## Requirements

- Python 3.10 or later (with tkinter -- see note below)
- A GPU is recommended for faster processing (NVIDIA with CUDA, or Apple Silicon with MPS), but not required

### Tested configurations

The installer auto-detects your CUDA version and installs the matching PyTorch build. These combinations have been verified to install and run correctly:

| Python | PyTorch | CUDA | GPU acceleration |
|--------|---------|------|------------------|
| 3.10 | cu118 | 11.x | Yes |
| 3.10 | cu124 | 12.x | Yes |
| 3.10 | cpu | - | No |
| 3.11 | cu118 | 11.x | Yes |
| 3.11 | cu124 | 12.x | Yes |
| 3.11 | cpu | - | No |
| 3.13 | cu124 | 12.x | Yes |
| 3.13 | cpu | - | No |

macOS uses the default PyPI torch build (MPS acceleration on Apple Silicon).

**tkinter note:** Photonarium uses tkinter for the native folder picker dialog. On Windows, make sure "tcl/tk and IDLE" is checked during Python installation (it is by default, but some minimal installs omit it). On Linux, install the `python3-tk` package (e.g. `sudo apt install python3-tk`). On macOS with Homebrew, `brew install python-tk@3.12`. The installer scripts will warn you if tkinter is missing.

## Quick install (recommended)

The installer scripts create a virtual environment, install all dependencies in the correct order, initialise the configuration, and download the ML models. They will ask where to store your data (database, thumbnails, config) and confirm before making changes.

**Windows:**

Open the Photonarium folder in File Explorer and double-click `install.bat`. If Windows SmartScreen shows a "Windows protected your PC" warning, click **More info** then **Run anyway** -- the script only installs Python packages and downloads ML models.

Alternatively, open Command Prompt, navigate to the Photonarium folder, and run:

```
install.bat
```

**Linux / macOS:**

Open a terminal, navigate to the Photonarium folder, and run:

```bash
chmod +x install.sh
./install.sh
```

The `chmod` command only needs to be run once (it marks the script as executable).

## Manual installation detail

If you prefer to install manually, or the installer script doesn't suit your setup, follow these steps.

1. **Create a virtual environment**

   ```bash
   python -m venv env
   ```

2. **Activate it**

   ```bash
   # Windows (Command Prompt)
   .\env\Scripts\activate

   # Windows (Git Bash / MinGW)
   . env/Scripts/Activate

   # Linux / macOS
   source env/bin/activate
   ```

3. **Install dependencies**

   ```bash
   # Upgrade pip
   python -m pip install --upgrade pip

   # PyTorch (with CUDA support for GPU acceleration)
   # Replace cu124 with cu118 for CUDA 11.x, or cpu for no GPU:
   pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124
   # macOS (use default PyPI -- the CUDA indexes have no macOS wheels):
   # pip install torch torchvision torchaudio

   # Other dependencies
   pip install open_clip_torch
   pip install pillow numpy pyyaml opencv-python imagehash flask waitress requests orjson transformers rawpy exifread

   # Install facenet-pytorch last with --no-deps to avoid its overly strict
   # version bounds on torch/numpy/pillow (the package is unmaintained)
   pip install --no-deps facenet-pytorch
   ```

4. **Initialise the configuration**

  This step is optional.

  Photonarium has various aspects of its behaviour which may be tuned. To do this, you might want to create the default configuration file first. This contains all of the standard settings along with comments to explain how they work.

  ```bash
  # Create the config file at the OS default location and exit
  python app/app.py --init-config .
  ```

  This will create a `photonarium.yml` configuration file at the OS-appropriate location (see [Configuration](#configuration) below). You can change settings later via the in-app **Edit Settings** button on the Database screen, or by editing the YAML file directly in a text editor.

5. **Download ML models**

   ```bash
   python download_models.py
   ```

   If you use a custom data directory, pass it here too so the aesthetic scoring model is stored in the right place:

   ```bash
   python download_models.py --data-dir /path/to/data
   ```

   This downloads the AI models required for image search, video processing, and captioning. Models are cached locally and only need to be downloaded once (or when you change model settings).

---

# Running Photonarium

```bash
python app/app.py
```

Then open `http://localhost:5000`

The app runs entirely offline after models are downloaded.

If you haven't looked already, take a look at the [Photonarium site](http://photonarium.org/tutorial/), and take a look at the tutorial.

By default, the server listens on all network interfaces (`0.0.0.0`), so other devices on your local network can reach it. To restrict access to this machine only, set `server_host: 127.0.0.1` in `photonarium.yml`.

**Important:** Photonarium is designed for use on a trusted home network. It has not been hardened for exposure to the public internet or untrusted networks. Do not make it accessible outside your local network -- doing so may introduce security risks that are outside the scope of this project.

## Command line options

```bash
python app/app.py --port 8080              # Use a different port
python app/app.py --data-dir /path/to/data # Override data directory for this session
python app/app.py --config /path/to/yml    # Use a specific config file
python app/app.py --init-config /data/dir  # Create config with data_dir set, then exit
python app/app.py --generate-thumbnails    # Pre-generate thumbnails for all images
python app/app.py --scan                   # Run folder scan on startup
python app/app.py --detect-faces           # Run face detection on startup
python app/app.py --group-faces            # Run unknown face grouping on startup
python app/app.py --scan --detect-faces    # Combine flags as needed
python app/app.py --extract-exif           # Extract EXIF metadata for all images and exit
python app/app.py --list-models            # Output required models as JSON (for scripting)
```

By default, no processing runs at startup. Add flags to opt in to the phases you want.

After running the installer (or `--init-config`), `python app.py` reads the data directory from the config file -- no `--data-dir` needed.

## Changing ML models

If you change model settings in `photonarium.yml`, run the model downloader again:

```bash
python download_models.py
```

Available caption models (from smallest to largest):

* `Salesforce/blip-image-captioning-base` (~1GB, fastest)
* `Salesforce/blip-image-captioning-large` (~2GB, default)
* `Salesforce/blip2-opt-2.7b` (~5GB, better quality)
* `Salesforce/blip2-flan-t5-xl` (~8GB, most descriptive)

---

# Configuration

Settings can be changed via the **Edit Settings** button on the Database screen, which opens an in-app editor that works from any device on your network. Settings are stored in `photonarium.yml` at the OS-appropriate location:

- **Windows:** `%LOCALAPPDATA%\Photonarium\photonarium.yml`
- **macOS:** `~/Library/Application Support/Photonarium/photonarium.yml`
- **Linux:** `~/.config/photonarium/photonarium.yml` (or `$XDG_CONFIG_HOME`)

The config file is created automatically on first run (or by the installer). Use `--config /path/to/file.yml` to override the location.

Key settings:

* `data_dir`: where Photonarium stores its database, thumbnails, and models (set by installer, overridable with `--data-dir`)
* `thumbnail_quality`: JPEG quality for thumbnails (1 to 100)
* `thumbnail_cache_size_mb`: RAM cache size for thumbnails
* `indexing_threads`: parallel threads for scanning
* `face_detection_enabled`: enable automatic face detection
* `face_detection_min_confidence`: detection confidence threshold
* `face_recognition_threshold`: default similarity threshold for auto-recognition (can be overridden per person in pick preferred mode)
* `caption_model`: BLIP model for image captioning (run `python download_models.py` after changing)
* `caption_max_length`: maximum caption length in tokens
* `caption_min_length`: minimum caption length (higher = more descriptive)
* `catalogue_dir`: path to the managed catalogue directory for imports (default: `<data-dir>/catalogue/`)
* `import_threads`: parallel threads for file copying during import (1-16, default 4)
* `trash_dir`: custom path for the trash directory (default: `<data-dir>/trash/`)

---

# Trash directory

When you delete images or videos (from the Gallery, full-screen viewer, or the Groups refine dialog), the files are moved to a trash directory instead of being permanently deleted. By default, this is `<data-dir>/trash/`.

* Files keep their original names; collisions get a counter suffix (`beach.jpg`, `beach (2).jpg`, etc.).
* The trash directory must not overlap any indexed folder. If it does, Photonarium disables trash operations and shows a warning.
* To recover a trashed file, move it back into an indexed folder and rescan.
* To customise the location, set `trash_dir` in `photonarium.yml`.
