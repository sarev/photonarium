@echo off
setlocal enabledelayedexpansion
:: =============================================================================
:: Photonarium Installer — Windows
::
:: Creates a Python virtual environment, installs all dependencies, initialises
:: the configuration, and downloads the required ML models.
::
:: Usage:
::   install.bat
:: =============================================================================

set "INSTALL_COMPLETE=0"
set "VENV_DIR=env"

:: Change to the directory containing this script
cd /d "%~dp0"

:: ---------------------------------------------------------------------------
:: 1. Find Python 3.10+
:: ---------------------------------------------------------------------------
set "PYTHON_CMD="

:: Try "python" first (most common on Windows)
where python >nul 2>&1
if !errorlevel! equ 0 (
    python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" 2>nul
    if !errorlevel! equ 0 (
        set "PYTHON_CMD=python"
    )
)

:: Try "python3"
if not defined PYTHON_CMD (
    where python3 >nul 2>&1
    if !errorlevel! equ 0 (
        python3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" 2>nul
        if !errorlevel! equ 0 (
            set "PYTHON_CMD=python3"
        )
    )
)

:: Try "py" (Windows Python Launcher)
if not defined PYTHON_CMD (
    where py >nul 2>&1
    if !errorlevel! equ 0 (
        py -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" 2>nul
        if !errorlevel! equ 0 (
            set "PYTHON_CMD=py"
        )
    )
)

if not defined PYTHON_CMD (
    echo.
    echo Python 3.10 or later is required but was not found.
    echo.
    echo Download Python from:
    echo   https://www.python.org/downloads/
    echo.
    echo   During installation, make sure to check "Add Python to PATH", and
    echo   make sure "tcl/tk and IDLE" is also checked in the installer.
    echo.
    echo.
    goto :error
)

:: Get the version string for display
for /f "delims=" %%v in ('%PYTHON_CMD% -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')"') do set "PYTHON_VERSION=%%v"
echo Detected platform: Windows
echo Using Python %PYTHON_VERSION% (%PYTHON_CMD%)

:: Check for tkinter (needed for the folder picker dialog)
%PYTHON_CMD% -c "import tkinter" 2>nul
if !errorlevel! neq 0 (
    echo.
    echo WARNING: tkinter is not installed. Photonarium uses it for the
    echo folder picker dialog. To fix this, re-run the Python installer,
    echo click "Modify", and make sure "tcl/tk and IDLE" is checked.
    echo.
)

:: ---------------------------------------------------------------------------
:: 1b. Detect CUDA version via nvidia-smi
:: ---------------------------------------------------------------------------
set "TORCH_VARIANT=cpu"
set "GPU_DISPLAY=No NVIDIA GPU detected"

where nvidia-smi >nul 2>&1
if !errorlevel! equ 0 (
    :: nvidia-smi prints "CUDA Version: X.Y" in its header — extract it
    for /f "tokens=*" %%a in ('nvidia-smi 2^>nul ^| findstr /C:"CUDA Version"') do (
        set "NVIDIA_LINE=%%a"
    )
    if defined NVIDIA_LINE (
        :: Extract the version number after "CUDA Version: "
        for /f "tokens=3 delims=: " %%v in ("!NVIDIA_LINE!") do (
            set "CUDA_FULL=%%v"
        )
        if defined CUDA_FULL (
            :: Extract major version (before the dot)
            for /f "tokens=1 delims=." %%m in ("!CUDA_FULL!") do (
                set "CUDA_MAJOR=%%m"
            )
            :: Get GPU name for display
            for /f "tokens=*" %%g in ('%PYTHON_CMD% -c "import subprocess; r=subprocess.run(['nvidia-smi','--query-gpu=name','--format=csv,noheader'], capture_output=True, text=True); print(r.stdout.strip().split(chr(10))[0])" 2^>nul') do (
                set "GPU_NAME=%%g"
            )
            if not defined GPU_NAME set "GPU_NAME=NVIDIA GPU"

            :: Map CUDA major version to PyTorch index
            if !CUDA_MAJOR! geq 12 (
                set "TORCH_VARIANT=cu124"
                set "GPU_DISPLAY=!GPU_NAME! ^(CUDA !CUDA_FULL! detected^)"
            ) else if !CUDA_MAJOR! equ 11 (
                set "TORCH_VARIANT=cu118"
                set "GPU_DISPLAY=!GPU_NAME! ^(CUDA !CUDA_FULL! detected^)"
            ) else (
                :: CUDA too old for PyTorch — fall back to CPU
                set "GPU_DISPLAY=!GPU_NAME! ^(CUDA !CUDA_FULL! — too old, using CPU^)"
            )
        )
    )
)

echo.
if "!TORCH_VARIANT!"=="cpu" (
    echo   GPU: !GPU_DISPLAY!
    echo   PyTorch: Installing CPU-only build
) else if "!TORCH_VARIANT!"=="cu118" (
    echo   GPU: !GPU_DISPLAY!
    echo   PyTorch: Installing with CUDA 11.8 acceleration
) else (
    echo   GPU: !GPU_DISPLAY!
    echo   PyTorch: Installing with CUDA 12.4 acceleration
)

:: ---------------------------------------------------------------------------
:: 2. Ask data directory
:: ---------------------------------------------------------------------------
echo.
echo Where should Photonarium store its data (database, thumbnails, config)?
echo.
echo   1) %LOCALAPPDATA%\photonarium
echo   2) %USERPROFILE%\Pictures\photonarium
echo   3) . (current directory)
echo   4) Custom path
echo.
set "DATA_CHOICE="
set /p "DATA_CHOICE=Choose [1-4, default=1]: "

if "!DATA_CHOICE!"=="2" (
    set "DATA_DIR=%USERPROFILE%\Pictures\photonarium"
) else if "!DATA_CHOICE!"=="3" (
    set "DATA_DIR=."
) else if "!DATA_CHOICE!"=="4" (
    set "CUSTOM_PATH="
    set /p "CUSTOM_PATH=Enter path: "
    if "!CUSTOM_PATH!"=="" (
        echo No path entered, using default.
        set "DATA_DIR=%LOCALAPPDATA%\photonarium"
    ) else (
        set "DATA_DIR=!CUSTOM_PATH!"
    )
) else (
    set "DATA_DIR=%LOCALAPPDATA%\photonarium"
)

:: Resolve and create directory if needed
if "!DATA_DIR!"=="." (
    set "DATA_DIR_FLAG="
    set "DATA_DIR_DISPLAY=current directory"
) else (
    if not exist "!DATA_DIR!" (
        set "CREATE_DIR="
        set /p "CREATE_DIR=Directory '!DATA_DIR!' does not exist. Create it? [Y/n]: "
        if /i "!CREATE_DIR!"=="n" (
            echo Aborting.
            goto :error
        )
        mkdir "!DATA_DIR!"
        if !errorlevel! neq 0 goto :error
        echo Created !DATA_DIR!
    )
    :: Resolve to absolute path
    pushd "!DATA_DIR!"
    set "DATA_DIR=!cd!"
    popd
    set "DATA_DIR_FLAG=--data-dir "!DATA_DIR!""
    set "DATA_DIR_DISPLAY=!DATA_DIR!"
)

:: ---------------------------------------------------------------------------
:: 3. Print summary and confirm
:: ---------------------------------------------------------------------------
echo.
echo ============================================================
echo   Photonarium Installer
echo ============================================================
echo.
echo   Platform:       Windows
echo   Python:         %PYTHON_VERSION% (%PYTHON_CMD%)
echo   Data directory: !DATA_DIR_DISPLAY!
echo   Virtual env:    .\%VENV_DIR%
echo.
echo   This will install:
echo     - PyTorch (with CUDA support where available)
echo     - OpenCLIP (image embeddings for semantic search)
echo     - BLIP (image captioning)
echo     - Face detection and recognition
echo     - Flask web server and utilities
echo.
echo   Disk space required: ~6-10 GB (mostly ML models)
echo   Model download may be slow on first run.
echo.
set "CONFIRM="
set /p "CONFIRM=Continue? [Y/n]: "
if /i "!CONFIRM!"=="n" (
    echo Installation cancelled.
    exit /b 0
)

:: ---------------------------------------------------------------------------
:: 4. Handle existing venv
:: ---------------------------------------------------------------------------
if exist "%VENV_DIR%\Scripts\activate.bat" (
    echo.
    echo An existing virtual environment was found at .\%VENV_DIR%
    set "RECREATE="
    set /p "RECREATE=Delete and recreate it? [Y/n]: "
    if /i "!RECREATE!"=="n" (
        echo Keeping existing virtual environment.
    ) else (
        echo Removing old virtual environment...
        rmdir /s /q "%VENV_DIR%"
    )
)

:: ---------------------------------------------------------------------------
:: Step 1/4: Create virtual environment
:: ---------------------------------------------------------------------------
echo.
echo ============================================================
echo   Step 1/4: Creating virtual environment
echo ============================================================

if not exist "%VENV_DIR%\Scripts\activate.bat" (
    %PYTHON_CMD% -m venv %VENV_DIR%
    if !errorlevel! neq 0 goto :error
    echo Created virtual environment at .\%VENV_DIR%
) else (
    echo Using existing virtual environment at .\%VENV_DIR%
)

:: Use explicit paths to venv binaries for all subsequent commands
set "VENV_PYTHON=%VENV_DIR%\Scripts\python.exe"
set "VENV_PIP=%VENV_DIR%\Scripts\pip.exe"

:: Verify the venv works
"%VENV_PYTHON%" -c "import sys; print(f'venv Python: {sys.executable}')"
if !errorlevel! neq 0 goto :error

:: ---------------------------------------------------------------------------
:: Step 2/4: Install dependencies
:: ---------------------------------------------------------------------------
echo.
echo ============================================================
echo   Step 2/4: Installing dependencies
echo ============================================================

echo.
echo --- Upgrading pip ---
"%VENV_PYTHON%" -m pip install --upgrade pip
if !errorlevel! neq 0 goto :error

echo.
echo --- Installing PyTorch ---
if "!TORCH_VARIANT!"=="cpu" (
    echo Installing CPU-only PyTorch...
    "%VENV_PIP%" install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cpu
    if !errorlevel! neq 0 goto :error
) else (
    echo Installing PyTorch with !TORCH_VARIANT! support...
    "%VENV_PIP%" install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/!TORCH_VARIANT!
    if !errorlevel! neq 0 (
        echo.
        echo CUDA build not available for this platform/Python version.
        echo Installing CPU-only PyTorch instead ^(Photonarium will still work,
        echo just without GPU acceleration^).
        echo.
        "%VENV_PIP%" install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cpu
        if !errorlevel! neq 0 goto :error
    )
)

echo.
echo --- Installing OpenCLIP ---
"%VENV_PIP%" install open_clip_torch
if !errorlevel! neq 0 goto :error

echo.
echo --- Installing remaining dependencies ---
"%VENV_PIP%" install pillow opencv-python imagehash numpy pyyaml flask waitress orjson requests transformers rawpy exifread
if !errorlevel! neq 0 goto :error

:: Install facenet-pytorch last with --no-deps to avoid its overly strict
:: version bounds on torch/numpy/pillow.  Suppress stderr so users don't
:: see the scary-looking (but harmless) pip dependency conflict warnings.
echo.
echo --- Installing face detection (facenet-pytorch) ---
"%VENV_PIP%" install --no-deps facenet-pytorch 2>nul
if !errorlevel! neq 0 goto :error
echo   Installed facenet-pytorch (with relaxed dependency bounds).

:: ---------------------------------------------------------------------------
:: Step 3/4: Initialise configuration
:: ---------------------------------------------------------------------------
echo.
echo ============================================================
echo   Step 3/4: Initialising configuration
echo ============================================================

if "!DATA_DIR_FLAG!"=="" (
    "%VENV_PYTHON%" app.py --init-config "."
) else (
    "%VENV_PYTHON%" app.py --init-config "!DATA_DIR!"
)
if !errorlevel! neq 0 goto :error
echo Configuration file created.

:: ---------------------------------------------------------------------------
:: Step 4/4: Download models
:: ---------------------------------------------------------------------------
echo.
echo ============================================================
echo   Step 4/4: Downloading ML models
echo ============================================================
echo.
echo This step downloads large model files and may take a while
echo depending on your internet connection.
echo.

:: Config now contains data_dir, so download_models.py reads it automatically
"%VENV_PYTHON%" download_models.py
if !errorlevel! neq 0 goto :error

:: ---------------------------------------------------------------------------
:: Final summary
:: ---------------------------------------------------------------------------
echo.
echo ============================================================
echo   Installation complete!
echo ============================================================
echo.

:: GPU availability check — write result to a temp file to avoid
:: single-quote conflicts between for /f and Python string literals
"%VENV_PYTHON%" -c "import torch; print(torch.cuda.is_available())" > "%TEMP%\photonarium_cuda.txt" 2>nul
set /p CUDA_AVAILABLE=<"%TEMP%\photonarium_cuda.txt"
del "%TEMP%\photonarium_cuda.txt" 2>nul

if "!CUDA_AVAILABLE!"=="True" (
    "%VENV_PYTHON%" -c "import torch; print(torch.cuda.get_device_name(0))" > "%TEMP%\photonarium_cuda.txt" 2>nul
    set /p CUDA_DEVICE=<"%TEMP%\photonarium_cuda.txt"
    del "%TEMP%\photonarium_cuda.txt" 2>nul
    echo   GPU: CUDA is available ^(!CUDA_DEVICE!^).
) else (
    echo   GPU: CUDA is not available. Photonarium will use the CPU.
    echo        For GPU acceleration, install NVIDIA drivers and CUDA toolkit:
    echo        https://developer.nvidia.com/cuda-downloads
)

echo.
echo   To start Photonarium, open a terminal in this folder and run:
echo.
echo     Command Prompt:
echo       %VENV_DIR%\Scripts\activate.bat
echo       python app.py
echo.
echo     PowerShell:
echo       %VENV_DIR%\Scripts\Activate.ps1
echo       python app.py
echo.
echo     If PowerShell blocks the script, run this first:
echo       Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
echo.
echo   Then open http://localhost:5000 in your browser.
echo.

set "INSTALL_COMPLETE=1"
echo.
echo Press any key to close this window.
pause >nul
goto :eof

:: ---------------------------------------------------------------------------
:: Error handler
:: ---------------------------------------------------------------------------
:error
echo.
echo ============================================================
echo   Installation failed.
echo   Check the messages above for details.
echo ============================================================
echo.
echo Press any key to close this window.
pause >nul
exit /b 1
