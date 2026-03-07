# Audit 17 — Avoid New Dependencies Without Strong Justification

## Principle

> Avoid new dependencies without strong justification

## Scope

- `docker/requirements-base.txt`, `docker/requirements-ml.txt` — Docker dependency lists
- `install.sh:383`, `install.bat:304` — installer dependency lists
- `docs/installation.md:426` — manual install documentation
- `CLAUDE.md` — collapsible install section
- `app/*.py` — import usage verification
- Video-related dependency additions

## Findings

### Dependency Synchronisation — All 6 Locations in Sync

| Location | Type | Status |
|----------|------|--------|
| `docker/requirements-base.txt:7-14` | Non-numpy packages | ✓ Synced |
| `docker/requirements-ml.txt:5-13` | Numpy-dependent / ML packages | ✓ Synced |
| `install.sh:383` | Linux/macOS pip install | ✓ Synced |
| `install.bat:304` | Windows pip install | ✓ Synced |
| `docs/installation.md:426` | Manual install docs | ✓ Synced |
| `CLAUDE.md:29-30` | Developer reference | ✓ Synced |

Docker correctly splits: `requirements-base.txt` has `av`, `ffmpeg-binaries` (non-numpy); `requirements-ml.txt` has `pillow`, `opencv-python-headless`, `faster-whisper` (numpy-dependent).

### Video Dependencies Added Correctly

Two dependencies added for video support (v1.2.0-beta.17):
- **`av`** (PyAV) — FFmpeg Python bindings. Lightweight pip-installable library for video frame extraction. Justified: essential for video processing; lighter than system ffmpeg.
- **`ffmpeg-binaries`** — Bundles FFmpeg/ffprobe executables. Justified: cross-platform binary distribution without requiring system package manager.

Both present in all 6 synchronisation locations.

### Speech-to-Text Dependency

**`faster-whisper`** — already listed in all locations. Used for video transcription in `app/stt.py`. Justified: only viable offline whisper implementation with good performance.

### No Unused Dependencies Found

Spot-checked imports across sampled files:
- `app.py`: All imports used (PIL, flask, config, imagedb, faces, etc.)
- `video.py`: Uses `subprocess`, `PIL`, `dataclass`, `datetime`, `Path`, `logging`, `av`
- `imagedb.py`: Comprehensive imports, all used
- No orphan packages detected

### Heavyweight Dependencies — All Justified

| Package | Size | Justification |
|---------|------|---------------|
| `torch` + `torchvision` | ~2GB | Core ML framework for OpenCLIP, NIMA, facenet |
| `transformers` | ~400MB | BLIP/BLIP-2 captioning |
| `opencv-python` | ~70MB | Video frame extraction, image processing |
| `av` | ~30MB | FFmpeg bindings for video I/O |
| `faster-whisper` | ~20MB | Offline speech-to-text |

No lightweight alternatives would significantly reduce footprint for the features provided.

### `facenet-pytorch` Isolation

Installed separately with `--no-deps` everywhere to avoid version conflicts — documented in all install locations.

## Status

**Compliant**

## Actions

None required.
