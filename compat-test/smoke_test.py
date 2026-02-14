"""
Smoke test — verifies that all key Photonarium dependencies import correctly.

Copied into each temp venv by run.py and executed there.  Prints a JSON blob
with package versions and import results so the runner can parse them.
"""
from __future__ import annotations

import json
import sys


def _try_import(module_name: str, version_attr: str = "__version__") -> dict:
    """Try to import a module and return its version or error."""
    try:
        mod = __import__(module_name)
        version = getattr(mod, version_attr, "unknown")
        return {"ok": True, "version": str(version)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _deep_test_blip() -> dict:
    """Exercise the specific BLIP imports Photonarium uses."""
    try:
        from transformers import BlipProcessor, BlipForConditionalGeneration  # noqa: F401
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _deep_test_blip2() -> dict:
    """Exercise the BLIP-2 imports (optional — may warn on newer transformers)."""
    try:
        from transformers import Blip2Processor, Blip2ForConditionalGeneration  # noqa: F401
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _check_cuda() -> dict:
    """Check CUDA availability and device name."""
    try:
        import torch
        available = torch.cuda.is_available()
        device = torch.cuda.get_device_name(0) if available else None
        return {"available": available, "device": device}
    except Exception as exc:
        return {"available": False, "error": str(exc)}


def main() -> None:
    results = {
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "imports": {
            "torch":            _try_import("torch"),
            "torchvision":      _try_import("torchvision"),
            "torchaudio":       _try_import("torchaudio"),
            "open_clip":        _try_import("open_clip"),
            "facenet_pytorch":  _try_import("facenet_pytorch"),
            "transformers":     _try_import("transformers"),
            "flask":            _try_import("flask"),
            "PIL":              _try_import("PIL", "PILLOW_VERSION"),
            "cv2":              _try_import("cv2"),
            "numpy":            _try_import("numpy"),
            "imagehash":        _try_import("imagehash"),
            "yaml":             _try_import("yaml"),
            "waitress":         _try_import("waitress"),
            "orjson":           _try_import("orjson"),
            "rawpy":            _try_import("rawpy"),
            "exifread":         _try_import("exifread"),
        },
        "deep": {
            "blip":  _deep_test_blip(),
            "blip2": _deep_test_blip2(),
        },
        "cuda": _check_cuda(),
    }

    # Pretty-print for human readability when run manually
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
