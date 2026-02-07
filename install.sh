#!/usr/bin/env bash
# =============================================================================
# Imaginary Installer — Linux / macOS
#
# Creates a Python virtual environment, installs all dependencies (with the
# correct torch variant for the platform), initialises the configuration,
# and downloads the required ML models.
#
# Usage:
#   chmod +x install.sh
#   ./install.sh
# =============================================================================

set -e

# ---------------------------------------------------------------------------
# Friendly error handler — triggered by set -e or the EXIT trap
# ---------------------------------------------------------------------------
_cleanup() {
    local exit_code=$?
    if [ "$exit_code" -ne 0 ] && [ "$INSTALL_COMPLETE" != "1" ]; then
        echo ""
        echo "============================================================"
        echo "  Installation failed (exit code $exit_code)."
        echo "  Check the messages above for details."
        echo "============================================================"
    fi
}
trap _cleanup EXIT

INSTALL_COMPLETE=0
VENV_DIR="env"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# ---------------------------------------------------------------------------
# 1. Detect platform
# ---------------------------------------------------------------------------
PLATFORM="$(uname -s)"
case "$PLATFORM" in
    Linux)  PLATFORM_NAME="Linux"  ;;
    Darwin) PLATFORM_NAME="macOS"  ;;
    *)
        echo "Unsupported platform: $PLATFORM"
        echo "This installer supports Linux and macOS. For Windows, use install.bat."
        exit 1
        ;;
esac
echo "Detected platform: $PLATFORM_NAME"

# ---------------------------------------------------------------------------
# 2. Find Python 3.11+
# ---------------------------------------------------------------------------
PYTHON_CMD=""

check_python_version() {
    # Returns 0 if the given command is Python >= 3.11
    local cmd="$1"
    if ! command -v "$cmd" &>/dev/null; then
        return 1
    fi
    "$cmd" -c "
import sys
if sys.version_info >= (3, 11):
    sys.exit(0)
else:
    sys.exit(1)
" 2>/dev/null
}

for candidate in python3 python; do
    if check_python_version "$candidate"; then
        PYTHON_CMD="$candidate"
        break
    fi
done

if [ -z "$PYTHON_CMD" ]; then
    echo ""
    echo "Python 3.11 or later is required but was not found."
    echo ""
    if [ "$PLATFORM_NAME" = "macOS" ]; then
        echo "Install Python from https://www.python.org/downloads/"
        echo "  or via Homebrew:  brew install python@3.11"
    else
        echo "Install Python using your package manager, for example:"
        echo "  Ubuntu/Debian:  sudo apt install python3.11 python3.11-venv"
        echo "  Fedora:         sudo dnf install python3.11"
        echo "  Arch:           sudo pacman -S python"
        echo "  or download from https://www.python.org/downloads/"
    fi
    exit 1
fi

PYTHON_VERSION="$("$PYTHON_CMD" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')")"
echo "Using Python $PYTHON_VERSION ($PYTHON_CMD)"

# ---------------------------------------------------------------------------
# 3. Ask data directory
# ---------------------------------------------------------------------------
echo ""
echo "Where should Imaginary store its data (database, thumbnails, config)?"
echo ""

if [ "$PLATFORM_NAME" = "macOS" ]; then
    DEFAULT_1="$HOME/Library/Application Support/imaginary"
    DEFAULT_2="$HOME/Pictures/imaginary"
else
    DEFAULT_1="$HOME/.local/share/imaginary"
    DEFAULT_2="$HOME/Pictures/imaginary"
fi
DEFAULT_3="."

echo "  1) $DEFAULT_1"
echo "  2) $DEFAULT_2"
echo "  3) $DEFAULT_3 (current directory)"
echo "  4) Custom path"
echo ""
read -rp "Choose [1-4, default=1]: " DATA_CHOICE

case "$DATA_CHOICE" in
    2)  DATA_DIR="$DEFAULT_2" ;;
    3)  DATA_DIR="$DEFAULT_3" ;;
    4)
        read -rp "Enter path: " DATA_DIR
        if [ -z "$DATA_DIR" ]; then
            echo "No path entered, using default."
            DATA_DIR="$DEFAULT_1"
        fi
        ;;
    *)  DATA_DIR="$DEFAULT_1" ;;
esac

# Resolve to absolute path (unless it's ".")
if [ "$DATA_DIR" != "." ]; then
    # Expand ~ if present
    DATA_DIR="${DATA_DIR/#\~/$HOME}"

    if [ ! -d "$DATA_DIR" ]; then
        read -rp "Directory '$DATA_DIR' does not exist. Create it? [Y/n]: " CREATE_DIR
        if [ -z "$CREATE_DIR" ] || [[ "$CREATE_DIR" =~ ^[Yy] ]]; then
            mkdir -p "$DATA_DIR"
            echo "Created '$DATA_DIR'"
        else
            echo "Aborting."
            exit 1
        fi
    fi

    DATA_DIR="$(cd "$DATA_DIR" && pwd)"
fi

# Build the --data-dir flag for later commands
if [ "$DATA_DIR" = "." ]; then
    DATA_DIR_FLAG=""
    DATA_DIR_DISPLAY="current directory"
else
    DATA_DIR_FLAG="--data-dir \"$DATA_DIR\""
    DATA_DIR_DISPLAY="$DATA_DIR"
fi

# ---------------------------------------------------------------------------
# 4. Print summary and confirm
# ---------------------------------------------------------------------------
echo ""
echo "============================================================"
echo "  Imaginary Installer"
echo "============================================================"
echo ""
echo "  Platform:       $PLATFORM_NAME"
echo "  Python:         $PYTHON_VERSION ($PYTHON_CMD)"
echo "  Data directory: $DATA_DIR_DISPLAY"
echo "  Virtual env:    ./$VENV_DIR"
echo ""
echo "  This will install:"
echo "    - PyTorch (with GPU support where available)"
echo "    - OpenCLIP (image embeddings for semantic search)"
echo "    - BLIP (image captioning)"
echo "    - Face detection and recognition"
echo "    - Flask web server and utilities"
echo ""
echo "  Disk space required: ~6-10 GB (mostly ML models)"
echo "  Model download may be slow on first run."
echo ""
read -rp "Continue? [Y/n]: " CONFIRM

if [[ "$CONFIRM" =~ ^[Nn] ]]; then
    echo "Installation cancelled."
    exit 0
fi

# ---------------------------------------------------------------------------
# 5. Handle existing venv
# ---------------------------------------------------------------------------
if [ -d "$VENV_DIR" ]; then
    echo ""
    echo "An existing virtual environment was found at ./$VENV_DIR"
    read -rp "Delete and recreate it? [Y/n]: " RECREATE
    if [ -z "$RECREATE" ] || [[ "$RECREATE" =~ ^[Yy] ]]; then
        echo "Removing old virtual environment..."
        rm -rf "$VENV_DIR"
    else
        echo "Keeping existing virtual environment."
    fi
fi

# ---------------------------------------------------------------------------
# Step 1/4: Create virtual environment
# ---------------------------------------------------------------------------
echo ""
echo "============================================================"
echo "  Step 1/4: Creating virtual environment"
echo "============================================================"

if [ ! -d "$VENV_DIR" ]; then
    "$PYTHON_CMD" -m venv "$VENV_DIR"
    echo "Created virtual environment at ./$VENV_DIR"
else
    echo "Using existing virtual environment at ./$VENV_DIR"
fi

# Use explicit paths to venv binaries for all subsequent commands
VENV_PYTHON="$VENV_DIR/bin/python"
VENV_PIP="$VENV_DIR/bin/pip"

# Verify the venv works
"$VENV_PYTHON" -c "import sys; print(f'venv Python: {sys.executable}')"

# ---------------------------------------------------------------------------
# Step 2/4: Install dependencies
# ---------------------------------------------------------------------------
echo ""
echo "============================================================"
echo "  Step 2/4: Installing dependencies"
echo "============================================================"

echo ""
echo "--- Upgrading pip ---"
"$VENV_PYTHON" -m pip install --upgrade pip

echo ""
echo "--- Installing PyTorch ---"
if [ "$PLATFORM_NAME" = "macOS" ]; then
    # macOS: install from default PyPI (includes MPS support)
    "$VENV_PIP" install torch torchvision torchaudio
else
    # Linux: install with CUDA support (cu124 wheels include CPU fallback)
    "$VENV_PIP" install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124
fi

echo ""
echo "--- Installing OpenCLIP ---"
"$VENV_PIP" install open_clip_torch

echo ""
echo "--- Installing face detection (facenet-pytorch) ---"
"$VENV_PIP" install --no-deps facenet-pytorch

echo ""
echo "--- Installing remaining dependencies ---"
"$VENV_PIP" install pillow opencv-python imagehash numpy pyyaml flask waitress orjson requests "transformers==4.44.*" rawpy exifread

# ---------------------------------------------------------------------------
# Step 3/4: Initialise configuration
# ---------------------------------------------------------------------------
echo ""
echo "============================================================"
echo "  Step 3/4: Initialising configuration"
echo "============================================================"

if [ -n "$DATA_DIR_FLAG" ]; then
    "$VENV_PYTHON" app.py --data-dir "$DATA_DIR" --list-models
else
    "$VENV_PYTHON" app.py --list-models
fi

echo "Configuration file created."

# ---------------------------------------------------------------------------
# Step 4/4: Download models
# ---------------------------------------------------------------------------
echo ""
echo "============================================================"
echo "  Step 4/4: Downloading ML models"
echo "============================================================"
echo ""
echo "This step downloads large model files and may take a while"
echo "depending on your internet connection."
echo ""

"$VENV_PYTHON" download_models.py

# ---------------------------------------------------------------------------
# Final summary
# ---------------------------------------------------------------------------
echo ""
echo "============================================================"
echo "  Installation complete!"
echo "============================================================"
echo ""

# GPU availability check
if [ "$PLATFORM_NAME" = "macOS" ]; then
    MPS_AVAILABLE="$("$VENV_PYTHON" -c "import torch; print('yes' if torch.backends.mps.is_available() else 'no')" 2>/dev/null || echo "no")"
    if [ "$MPS_AVAILABLE" = "yes" ]; then
        echo "  GPU: Apple MPS acceleration is available."
    else
        echo "  GPU: Apple MPS is not available. Imaginary will use the CPU."
        echo "       (MPS requires macOS 12.3+ and Apple Silicon or supported AMD GPU)"
    fi
else
    CUDA_AVAILABLE="$("$VENV_PYTHON" -c "import torch; print('yes' if torch.cuda.is_available() else 'no')" 2>/dev/null || echo "no")"
    if [ "$CUDA_AVAILABLE" = "yes" ]; then
        CUDA_DEVICE="$("$VENV_PYTHON" -c "import torch; print(torch.cuda.get_device_name(0))" 2>/dev/null || echo "unknown")"
        echo "  GPU: CUDA is available ($CUDA_DEVICE)."
    else
        echo "  GPU: CUDA is not available. Imaginary will use the CPU."
        echo "       For GPU acceleration, install NVIDIA drivers and CUDA toolkit:"
        echo "       https://developer.nvidia.com/cuda-downloads"
    fi
fi

echo ""
echo "  To start Imaginary:"
echo ""
echo "    source $VENV_DIR/bin/activate"
if [ -n "$DATA_DIR_FLAG" ]; then
    echo "    python app.py --data-dir \"$DATA_DIR\""
else
    echo "    python app.py"
fi
echo ""
echo "  Then open http://localhost:5000 in your browser."
echo ""

INSTALL_COMPLETE=1
