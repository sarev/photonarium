# Audit 03 — Cross-Platform

## Principle

> Cross-platform: Windows, Mac, Linux

## Scope

- `app/*.py` — path handling, OS-specific APIs, subprocess calls, signal handling
- `app/config.py` — OS-aware config path resolution
- `app/thumbnails.py` — Windows file locking
- `app/video.py` — ffmpeg/ffprobe binary resolution
- `app/metadata.py` — file timestamp handling
- `app/app.py` — platform-specific file reveal
- `install.sh`, `install.bat` — installers

## Findings

1. **Path handling** — consistent `pathlib.Path` usage throughout:
   - `rawimage.py:37,108,138,198`, `thumbnails.py:41,53,127,158`, `config.py:35,49-65`, `app.py:199,778,1480-1486`
   - `duplicates.py:84-85` — explicit cross-platform normalisation: `os.path.normpath()` then `replace('\\', '/')` for display

2. **OS-aware config paths** (`config.py:49-65`):
   - Windows: `%LOCALAPPDATA%\Photonarium\photonarium.yml`
   - macOS: `~/Library/Application Support/Photonarium/photonarium.yml`
   - Linux: `$XDG_CONFIG_HOME/photonarium/` or `~/.config/photonarium/`

3. **Platform-specific file reveal** (`app.py:1050-1076`):
   - Windows: `explorer /select,{path}`
   - macOS: `open -R {path}`
   - Linux: `xdg-open` with WSL2 fallback to `explorer.exe`

4. **File timestamp handling** (`metadata.py:1710-1724`):
   - Windows: uses `st_ctime` (creation time)
   - Unix/macOS: attempts `st_birthtime` (macOS/BSD), falls back to `st_mtime`

5. **Windows file locking** (`thumbnails.py:95-107`):
   - Catches `winerror == 32` (sharing violation) with exponential backoff retry — Windows-only guard with `sys.platform == 'win32'` check

6. **Cross-platform subprocess** (`video.py:173-182, 396-419`):
   - Uses `shutil.which('ffprobe')` / `shutil.which('ffmpeg')` for binary resolution
   - Graceful degradation (returns zero/empty) if binaries not found

7. **Signal handling concern** (`imagedb.py:8648-8649`):
   - Registers `signal.SIGINT` and `signal.SIGTERM` handlers directly
   - `SIGTERM` exists on Windows but may behave differently
   - `SIGINT` works on Windows for Ctrl+C
   - In practice this is safe since Waitress handles shutdown, but defensive `hasattr` checks would be cleaner

## Status

**Mostly Compliant**

## Actions

- **P3**: Add defensive guard around `signal.SIGTERM` registration at `imagedb.py:8649` — wrap in `hasattr(signal, 'SIGTERM')` or try/except for robustness on unusual platforms
