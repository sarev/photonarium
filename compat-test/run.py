"""
Compatibility matrix tester for Photonarium's installation flexibility.

Tests install+import across combinations of Python versions, PyTorch CUDA
variants, and transformers versions.  Each combination gets a fresh temp venv,
installs all dependencies in installer order, runs smoke_test.py inside it,
and records pass/fail plus installed versions.

Usage (from the project root):
    python compat-test/run.py              # Run all combos
    python compat-test/run.py --combo 3    # Run only combo #3 (1-indexed)
    python compat-test/run.py --list       # List combos without running

All artifacts (venvs, results) are written to compat-test/ subdirectory.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Directory layout
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
VENVS_DIR = SCRIPT_DIR / "venvs"
RESULTS_FILE = SCRIPT_DIR / "results.txt"
SMOKE_TEST = SCRIPT_DIR / "smoke_test.py"

IS_WINDOWS = platform.system() == "Windows"

# The test matrix — each entry describes a realistic user scenario.
# "torch_variant" is one of: cu118, cu124, cpu, default (macOS PyPI)
# "transformers" is either "pinned" (==4.44.*) or "latest"
COMBOS: list[dict] = [
    {"python": "3.10", "torch": "cu118",    "transformers": "pinned",  "note": "Ubuntu 22.04 + older NVIDIA"},
    {"python": "3.10", "torch": "cu124",    "transformers": "pinned",  "note": "Ubuntu 22.04 + current NVIDIA"},
    {"python": "3.10", "torch": "cpu",      "transformers": "latest",  "note": "Ubuntu 22.04, no GPU"},
    {"python": "3.11", "torch": "cu124",    "transformers": "pinned",  "note": "Current recommended setup"},
    {"python": "3.11", "torch": "cu118",    "transformers": "latest",  "note": "Existing users upgrading transformers"},
    {"python": "3.11", "torch": "cpu",      "transformers": "latest",  "note": "Mac/laptop users"},
    {"python": "3.13", "torch": "cu124",    "transformers": "pinned",  "note": "Latest Python + current NVIDIA"},
    {"python": "3.13", "torch": "cu124",    "transformers": "latest",  "note": "Latest everything"},
    {"python": "3.13", "torch": "cpu",      "transformers": "latest",  "note": "Latest Python, no GPU"},
]


# ---------------------------------------------------------------------------
# Python interpreter discovery
# ---------------------------------------------------------------------------

def _find_python_interpreters() -> dict[str, str]:
    """
    Discover available Python interpreters on this machine.

    Returns a dict mapping version strings like "3.10" to executable paths.
    """
    found: dict[str, str] = {}

    # Candidates to check on PATH
    candidates = ["python3", "python", "py"]
    if IS_WINDOWS:
        # Windows Python Launcher can target specific versions
        candidates.extend([f"py -{v}" for v in ("3.10", "3.11", "3.12", "3.13")])

    # Also check common install directories
    if IS_WINDOWS:
        # Standard Python install paths on Windows
        for base in [
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Python",
            Path("C:/Python"),
            Path("C:/Program Files/Python"),
        ]:
            if base.exists():
                for child in base.iterdir():
                    exe = child / "python.exe"
                    if exe.exists():
                        candidates.append(str(exe))
    else:
        # Common Linux/macOS paths
        for v in ("3.10", "3.11", "3.12", "3.13"):
            for prefix in ("/usr/bin", "/usr/local/bin", f"/opt/python/{v}/bin"):
                candidates.append(f"{prefix}/python{v}")

    for candidate in candidates:
        try:
            parts = candidate.split()
            result = subprocess.run(
                [*parts, "-c",
                 "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}'); "
                 "print(sys.executable)"],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                lines = result.stdout.strip().splitlines()
                if len(lines) >= 2:
                    version = lines[0].strip()
                    exe_path = lines[1].strip()
                    if version not in found:
                        found[version] = exe_path
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            continue

    return found


# ---------------------------------------------------------------------------
# Venv creation and dependency installation
# ---------------------------------------------------------------------------

def _run_cmd(cmd: list[str], label: str, cwd: str | None = None,
             suppress_stderr: bool = False) -> tuple[bool, str]:
    """
    Run a command with real-time output streaming.

    Output is printed line-by-line so the user can see download progress,
    package resolution, etc. during long pip installs.  When suppress_stderr
    is True, stderr is discarded (used for facenet-pytorch to hide the
    harmless dependency conflict warnings).
    """
    print(f"\n    {label}...", flush=True)
    collected: list[str] = []
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL if suppress_stderr else subprocess.STDOUT,
            text=True,
            bufsize=1,         # line-buffered
            cwd=cwd,
        )
        # Stream output line-by-line so the user sees progress in real time
        for line in proc.stdout:
            stripped = line.rstrip("\n")
            print(f"      {stripped}", flush=True)
            collected.append(stripped)
        proc.wait(timeout=600)
        ok = proc.returncode == 0
        status = "OK" if ok else f"FAILED (exit {proc.returncode})"
        print(f"    -> {status}", flush=True)
        return ok, "\n".join(collected)
    except subprocess.TimeoutExpired:
        proc.kill()
        print("    -> TIMEOUT", flush=True)
        return False, "Command timed out after 600s"
    except Exception as exc:
        print(f"    -> ERROR: {exc}", flush=True)
        return False, str(exc)


def _get_venv_python(venv_dir: Path) -> str:
    """Return the path to the Python executable inside a venv."""
    if IS_WINDOWS:
        return str(venv_dir / "Scripts" / "python.exe")
    return str(venv_dir / "bin" / "python")


def _get_venv_pip(venv_dir: Path) -> str:
    """Return the path to pip inside a venv."""
    if IS_WINDOWS:
        return str(venv_dir / "Scripts" / "pip.exe")
    return str(venv_dir / "bin" / "pip")


def _torch_index_url(variant: str) -> str | None:
    """Return the --index-url for a torch variant, or None for default PyPI."""
    urls = {
        "cu118": "https://download.pytorch.org/whl/cu118",
        "cu124": "https://download.pytorch.org/whl/cu124",
        "cpu":   "https://download.pytorch.org/whl/cpu",
    }
    return urls.get(variant)


def run_combo(combo_idx: int, combo: dict, python_exe: str) -> dict:
    """
    Run a single test combination.

    Creates a temp venv, installs deps, runs smoke_test.py, and cleans up.
    Returns a result dict with pass/fail, versions, and any errors.
    """
    label = (f"Combo {combo_idx}: Python {combo['python']}, "
             f"torch={combo['torch']}, transformers={combo['transformers']}")
    print(f"\n{'=' * 70}")
    print(f"  {label}")
    print(f"  ({combo['note']})")
    print(f"{'=' * 70}")

    venv_dir = VENVS_DIR / f"venv_{combo_idx}"
    result: dict = {
        "combo": combo_idx,
        "config": combo.copy(),
        "python_exe": python_exe,
        "steps": {},
        "smoke_test": None,
        "overall": "FAIL",
    }

    start_time = time.time()

    try:
        # Clean up any leftover venv from a previous run
        if venv_dir.exists():
            shutil.rmtree(venv_dir, ignore_errors=True)

        # Step 1: Create venv
        ok, output = _run_cmd(
            [python_exe, "-m", "venv", str(venv_dir)],
            "Creating venv"
        )
        result["steps"]["create_venv"] = {"ok": ok, "output": output[:500]}
        if not ok:
            return result

        venv_python = _get_venv_python(venv_dir)
        venv_pip = _get_venv_pip(venv_dir)

        # Step 2: Upgrade pip
        ok, output = _run_cmd(
            [venv_python, "-m", "pip", "install", "--upgrade", "pip"],
            "Upgrading pip"
        )
        result["steps"]["upgrade_pip"] = {"ok": ok}
        if not ok:
            return result

        # Step 3: Install PyTorch
        torch_url = _torch_index_url(combo["torch"])
        torch_cmd = [venv_pip, "install", "torch", "torchvision", "torchaudio"]
        if torch_url:
            torch_cmd.extend(["--index-url", torch_url])

        ok, output = _run_cmd(torch_cmd, f"Installing PyTorch ({combo['torch']})")
        result["steps"]["install_torch"] = {"ok": ok, "output": output[-1000:]}
        if not ok:
            return result

        # Step 4: Install OpenCLIP
        ok, output = _run_cmd(
            [venv_pip, "install", "open_clip_torch"],
            "Installing OpenCLIP"
        )
        result["steps"]["install_openclip"] = {"ok": ok}
        if not ok:
            return result

        # Step 5: Install remaining deps (everything except facenet-pytorch)
        transformers_spec = "transformers==4.44.*" if combo["transformers"] == "pinned" else "transformers"
        deps = [
            "pillow", "opencv-python", "imagehash", "numpy", "pyyaml",
            "flask", "waitress", "orjson", "requests",
            transformers_spec,
            "rawpy", "exifread",
        ]
        ok, output = _run_cmd(
            [venv_pip, "install"] + deps,
            f"Installing remaining deps (transformers={combo['transformers']})"
        )
        result["steps"]["install_deps"] = {"ok": ok, "output": output[-1000:]}
        if not ok:
            return result

        # Step 6: Install facenet-pytorch LAST with --no-deps (suppress stderr)
        ok, output = _run_cmd(
            [venv_pip, "install", "--no-deps", "facenet-pytorch"],
            "Installing facenet-pytorch (--no-deps)",
            suppress_stderr=True,
        )
        result["steps"]["install_facenet"] = {"ok": ok}
        if not ok:
            return result

        # Step 7: Run smoke test
        print(f"    Running smoke test...", end=" ", flush=True)
        try:
            smoke_result = subprocess.run(
                [venv_python, str(SMOKE_TEST)],
                capture_output=True, text=True, timeout=120,
            )
            if smoke_result.returncode == 0:
                smoke_data = json.loads(smoke_result.stdout)
                result["smoke_test"] = smoke_data
                # Check if all imports succeeded
                all_ok = all(
                    v["ok"] for v in smoke_data.get("imports", {}).values()
                )
                print("OK" if all_ok else "PARTIAL (some imports failed)")
                result["overall"] = "PASS" if all_ok else "PARTIAL"
            else:
                print(f"FAILED (exit {smoke_result.returncode})")
                result["smoke_test"] = {
                    "error": smoke_result.stderr[:500] or smoke_result.stdout[:500]
                }
        except subprocess.TimeoutExpired:
            print("TIMEOUT")
            result["smoke_test"] = {"error": "Smoke test timed out"}
        except json.JSONDecodeError as exc:
            print(f"PARSE ERROR: {exc}")
            result["smoke_test"] = {"error": f"JSON parse error: {exc}"}

    finally:
        elapsed = time.time() - start_time
        result["elapsed_seconds"] = round(elapsed, 1)
        print(f"    Elapsed: {elapsed:.1f}s")

        # Clean up the venv to save disk space
        if venv_dir.exists():
            print(f"    Cleaning up venv...", end=" ", flush=True)
            shutil.rmtree(venv_dir, ignore_errors=True)
            print("done")

    return result


# ---------------------------------------------------------------------------
# Results summary
# ---------------------------------------------------------------------------

def _print_summary(results: list[dict]) -> None:
    """Print a summary table of all results."""
    print(f"\n{'=' * 78}")
    print("  RESULTS SUMMARY")
    print(f"{'=' * 78}")
    print()

    # Header
    header = f"{'#':>3}  {'Python':>6}  {'Torch':>6}  {'Transformers':>13}  {'Result':>8}  {'Time':>6}  Note"
    print(header)
    print("-" * len(header))

    for r in results:
        cfg = r.get("config", {})
        num = r.get("combo", "?")
        py = cfg.get("python", "?")
        torch_v = cfg.get("torch", "?")
        tf = cfg.get("transformers", "?")
        overall = r.get("overall", "SKIP")
        elapsed = r.get("elapsed_seconds", 0)
        note = cfg.get("note", "")
        skip_reason = r.get("skip_reason", "")

        status = overall
        if skip_reason:
            note = f"SKIP: {skip_reason}"

        print(f"{num:>3}  {py:>6}  {torch_v:>6}  {tf:>13}  {status:>8}  {elapsed:>5.0f}s  {note}")

    # Import details for non-passing combos
    failures = [r for r in results if r.get("overall") not in ("PASS", "SKIP")]
    if failures:
        print(f"\n{'─' * 78}")
        print("  FAILURE DETAILS")
        print(f"{'─' * 78}")
        for r in failures:
            cfg = r.get("config", {})
            print(f"\n  Combo {r['combo']}: Python {cfg.get('python')}, "
                  f"torch={cfg.get('torch')}, transformers={cfg.get('transformers')}")

            smoke = r.get("smoke_test")
            if smoke and isinstance(smoke, dict):
                if "error" in smoke:
                    print(f"    Smoke test error: {smoke['error']}")
                elif "imports" in smoke:
                    for pkg, info in smoke["imports"].items():
                        if not info.get("ok"):
                            print(f"    FAIL: {pkg} — {info.get('error', '?')}")
                deep = smoke.get("deep", {})
                for name, info in deep.items():
                    if not info.get("ok"):
                        print(f"    FAIL (deep): {name} — {info.get('error', '?')}")

            # Print step failures
            for step_name, step_info in r.get("steps", {}).items():
                if isinstance(step_info, dict) and not step_info.get("ok"):
                    output_preview = step_info.get("output", "")[:300]
                    print(f"    Step '{step_name}' failed: {output_preview}")

    # Version details for passing combos
    passes = [r for r in results if r.get("overall") == "PASS"]
    if passes:
        print(f"\n{'─' * 78}")
        print("  INSTALLED VERSIONS (passing combos)")
        print(f"{'─' * 78}")
        for r in passes:
            cfg = r.get("config", {})
            smoke = r.get("smoke_test", {})
            imports = smoke.get("imports", {})
            versions = {pkg: info.get("version", "?")
                        for pkg, info in imports.items() if info.get("ok")}
            cuda = smoke.get("cuda", {})

            print(f"\n  Combo {r['combo']}: Python {cfg.get('python')}, "
                  f"torch={cfg.get('torch')}, transformers={cfg.get('transformers')}")
            key_pkgs = ["torch", "transformers", "numpy", "PIL", "open_clip"]
            for pkg in key_pkgs:
                if pkg in versions:
                    print(f"    {pkg}: {versions[pkg]}")
            if cuda.get("available"):
                print(f"    CUDA: {cuda.get('device', 'available')}")

    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Photonarium compatibility matrix tester",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Each combo creates a fresh venv, installs all dependencies, runs
            an import smoke test, and cleans up.  Results are written to
            compat-test/results.txt.
        """),
    )
    parser.add_argument(
        "--combo", type=int, default=0,
        help="Run only this combo number (1-indexed). 0 = all.",
    )
    parser.add_argument(
        "--list", action="store_true",
        help="List all combos without running them.",
    )
    args = parser.parse_args()

    # List mode
    if args.list:
        print(f"\n{'#':>3}  {'Python':>6}  {'Torch':>6}  {'Transformers':>13}  Note")
        print("-" * 70)
        for i, combo in enumerate(COMBOS, 1):
            print(f"{i:>3}  {combo['python']:>6}  {combo['torch']:>6}  "
                  f"{combo['transformers']:>13}  {combo['note']}")
        print()
        return

    # Discover Python interpreters
    print("Discovering Python interpreters...")
    interpreters = _find_python_interpreters()
    if not interpreters:
        print("ERROR: No Python interpreters found!")
        sys.exit(1)

    print(f"  Found: {', '.join(f'{v} ({p})' for v, p in sorted(interpreters.items()))}")

    # Create venvs directory
    VENVS_DIR.mkdir(parents=True, exist_ok=True)

    # Determine which combos to run
    if args.combo:
        if args.combo < 1 or args.combo > len(COMBOS):
            print(f"ERROR: --combo must be between 1 and {len(COMBOS)}")
            sys.exit(1)
        combos_to_run = [(args.combo, COMBOS[args.combo - 1])]
    else:
        combos_to_run = list(enumerate(COMBOS, 1))

    # Run each combo
    all_results: list[dict] = []
    total_start = time.time()

    for combo_idx, combo in combos_to_run:
        python_version = combo["python"]
        if python_version not in interpreters:
            print(f"\n  Combo {combo_idx}: SKIP — Python {python_version} not found")
            all_results.append({
                "combo": combo_idx,
                "config": combo,
                "overall": "SKIP",
                "skip_reason": f"Python {python_version} not available",
                "elapsed_seconds": 0,
            })
            continue

        python_exe = interpreters[python_version]
        result = run_combo(combo_idx, combo, python_exe)
        all_results.append(result)

    total_elapsed = time.time() - total_start

    # Print summary
    _print_summary(all_results)
    print(f"  Total time: {total_elapsed:.0f}s ({total_elapsed / 60:.1f} min)")

    # Write results to file
    with open(RESULTS_FILE, "w", encoding="utf-8") as f:
        f.write(f"Photonarium Compatibility Test Results\n")
        f.write(f"{'=' * 50}\n")
        f.write(f"Date: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"Platform: {platform.system()} {platform.release()}\n")
        f.write(f"Runner Python: {sys.version}\n")
        f.write(f"Total time: {total_elapsed:.0f}s\n\n")
        f.write(json.dumps(all_results, indent=2))

    print(f"  Results written to: {RESULTS_FILE}")

    # Exit with failure if any combo failed
    any_fail = any(r.get("overall") == "FAIL" for r in all_results)
    sys.exit(1 if any_fail else 0)


if __name__ == "__main__":
    main()
