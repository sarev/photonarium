"""Setup wizard subprocess management.

Manages a single ``download_models.py`` subprocess for the setup wizard,
providing start/stop/status operations.  The frontend polls ``get_status()``
to display real-time download output in the wizard dialog.
"""

from __future__ import annotations

import atexit
import logging
import os
import signal
import subprocess
import sys
import threading
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level state — guarded by _lock
# ---------------------------------------------------------------------------

_process: subprocess.Popen | None = None
_output_lines: list[str] = []
_state: str = 'idle'  # idle | running | completed | failed | aborted
_return_code: int | None = None
_lock = threading.Lock()
_atexit_registered = False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def start_download(config_path: str, data_dir: str, hf_token: str | None = None) -> dict[str, str]:
    """Launch ``download_models.py`` as a subprocess.

    The process inherits the current Python interpreter and runs in the
    repository root (one level up from ``app/``).  A daemon thread reads
    stdout line-by-line into ``_output_lines`` so the frontend can poll
    for incremental output.

    Args:
        config_path: Absolute path to the YAML config file.
        data_dir: Absolute path to the data directory.
        hf_token: Optional HuggingFace access token for authenticated
            downloads.  Passed via ``HF_TOKEN`` environment variable.

    Returns:
        ``{'status': 'started'}`` on success.

    Raises:
        RuntimeError: If a download is already in progress.
    """
    global _process, _output_lines, _state, _return_code, _atexit_registered

    with _lock:
        if _state == 'running':
            raise RuntimeError('A download is already in progress')

        _output_lines = []
        _state = 'running'
        _return_code = None

        # download_models.py lives in the repo root (parent of app/)
        app_dir = os.path.dirname(os.path.abspath(__file__))
        repo_root = os.path.dirname(app_dir)
        script = os.path.join(repo_root, 'download_models.py')

        cmd = [
            sys.executable,
            script,
            '--config',
            config_path,
            '--data-dir',
            data_dir,
        ]

        logger.info('Wizard: starting model download: %s', ' '.join(cmd))

        # The running app sets HF_HUB_OFFLINE=1 (in imagedb.py / caption.py)
        # to prevent accidental network calls during normal operation.  The
        # download script *needs* network access, so we override it to '0'.
        env = os.environ.copy()
        env['HF_HUB_OFFLINE'] = '0'
        if hf_token:
            env['HF_TOKEN'] = hf_token

        _process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=repo_root,
            env=env,
        )

        # Daemon thread to read output
        reader = threading.Thread(
            target=_read_output,
            args=(_process,),
            daemon=True,
            name='wizard-download-reader',
        )
        reader.start()

        # Register cleanup once
        if not _atexit_registered:
            atexit.register(_cleanup)
            _atexit_registered = True

    return {'status': 'started'}


def get_status(since_line: int = 0) -> dict[str, Any]:
    """Return current download state and new output lines.

    Args:
        since_line: Line index to start from (for incremental polling).

    Returns:
        Dict with ``state``, ``lines`` (new lines since *since_line*),
        ``total_lines``, and ``return_code``.
    """
    with _lock:
        return {
            'state': _state,
            'lines': _output_lines[since_line:],
            'total_lines': len(_output_lines),
            'return_code': _return_code,
        }


def abort_download() -> dict[str, str]:
    """Abort a running download subprocess.

    Sends SIGTERM (or ``terminate()`` on Windows), waits 3 seconds, then
    sends SIGKILL if the process is still alive.

    Returns:
        ``{'status': 'aborted'}`` on success, or ``{'status': 'not_running'}``
        if no download is in progress.
    """
    global _state

    with _lock:
        if _process is None or _state != 'running':
            return {'status': 'not_running'}

        logger.info('Wizard: aborting model download')
        _state = 'aborted'
        proc = _process

    # Terminate outside the lock to avoid blocking
    try:
        proc.terminate()
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
    except Exception:
        logger.exception('Wizard: error killing download process')

    return {'status': 'aborted'}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _read_output(proc: subprocess.Popen) -> None:
    """Read subprocess stdout line-by-line into ``_output_lines``.

    Sets ``_state`` to ``completed`` or ``failed`` when the process exits,
    unless it was already set to ``aborted``.
    """
    global _state, _return_code

    try:
        for line in proc.stdout:
            stripped = line.rstrip('\n\r')
            with _lock:
                _output_lines.append(stripped)
    except Exception:
        logger.exception('Wizard: error reading download output')
    finally:
        proc.wait()
        with _lock:
            _return_code = proc.returncode
            # Don't overwrite 'aborted' state
            if _state == 'running':
                _state = 'completed' if proc.returncode == 0 else 'failed'
        logger.info('Wizard: download process exited with code %s (state=%s)', proc.returncode, _state)


def _cleanup() -> None:
    """Atexit handler: kill any orphan download process."""
    if _process is not None and _process.poll() is None:
        logger.info('Wizard: cleaning up orphan download process')
        try:
            _process.terminate()
            _process.wait(timeout=3)
        except Exception:
            try:
                if sys.platform != 'win32':
                    _process.send_signal(signal.SIGKILL)
                else:
                    _process.kill()
            except Exception:
                pass
