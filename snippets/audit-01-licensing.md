# Audit 01 — Apache 2.0 FOSS Licence Compatible

## Principle

> Apache 2.0 FOSS licence compatible

## Scope

- `LICENSES.md` — primary licence declaration and dependency matrix
- `docker/requirements-base.txt`, `docker/requirements-ml.txt` — all pip packages
- `install.sh:383`, `install.bat:304` — installer dependency lists
- `app/` Python source — checked for vendored code, GPL imports, licence headers
- Pre-trained model licences (OpenCLIP, BLIP, LAION, NIMA, VGGFace2)

## Findings

1. **Primary licence** (`LICENSES.md:3-16`): Apache License 2.0, Copyright 2024-2026 7th software Ltd. Comprehensive declaration with full text reference.

2. **Dependency compatibility** (`LICENSES.md:20-80`): All 30+ dependencies verified as permissively licensed:
   - Framework: Flask (BSD-3), Waitress (ZPL-2.1), Python (PSF)
   - ML: PyTorch (BSD-3), OpenCLIP (MIT), HuggingFace Transformers (Apache 2.0), facenet-pytorch (MIT)
   - Image: Pillow (HPND), OpenCV (Apache 2.0), rawpy (MIT), ImageHash (BSD-2)
   - Utilities: NumPy (BSD-3), orjson (Apache 2.0/MIT), PyYAML (MIT), Requests (Apache 2.0)
   - Video: av/PyAV (BSD-3), ffmpeg-binaries (Apache 2.0), faster-whisper (MIT)

3. **No GPL contamination**: Zero GPL-licensed packages found across all requirement files and install scripts.

4. **VGGFace2 advisory** (`LICENSES.md:52-54`): Training dataset is CC BY-NC 4.0, but the model weights (facenet-pytorch) are MIT-licensed. Commercial use advisory is documented.

5. **No vendored third-party code**: No embedded source from external projects found.

6. **Source file headers**: Files use module-level docstrings rather than per-file Apache 2.0 boilerplate. Repository-level `LICENSES.md` provides coverage. This is acceptable under Apache 2.0 (NOTICE file approach).

## Status

**Compliant**

## Actions

None required.
