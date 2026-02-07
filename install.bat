@echo off
setlocal enabledelayedexpansion
:: =============================================================================
:: Imaginary Installer — Windows
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
:: 1. Find Python 3.11+
:: ---------------------------------------------------------------------------
set "PYTHON_CMD="

:: Try "python" first (most common on Windows)
where python >nul 2>&1
if !errorlevel! equ 0 (
    python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" 2>nul
    if !errorlevel! equ 0 (
        set "PYTHON_CMD=python"
    )
)

:: Try "python3"
if not defined PYTHON_CMD (
    where python3 >nul 2>&1
    if !errorlevel! equ 0 (
        python3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" 2>nul
        if !errorlevel! equ 0 (
            set "PYTHON_CMD=python3"
        )
    )
)

:: Try "py" (Windows Python Launcher)
if not defined PYTHON_CMD (
    where py >nul 2>&1
    if !errorlevel! equ 0 (
        py -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" 2>nul
        if !errorlevel! equ 0 (
            set "PYTHON_CMD=py"
        )
    )
)

if not defined PYTHON_CMD (
    echo.
    echo Python 3.11 or later is required but was not found.
    echo.
    echo Download Python from https://www.python.org/downloads/
    echo   - Make sure to check "Add Python to PATH" during installation.
    echo   - If Python is already installed, check that it is on your PATH.
    echo.
    goto :error
)

:: Get the version string for display
for /f "delims=" %%v in ('%PYTHON_CMD% -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')"') do set "PYTHON_VERSION=%%v"
echo Detected platform: Windows
echo Using Python %PYTHON_VERSION% (%PYTHON_CMD%)

:: ---------------------------------------------------------------------------
:: 2. Ask data directory
:: ---------------------------------------------------------------------------
echo.
echo Where should Imaginary store its data (database, thumbnails, config)?
echo.
echo   1) %LOCALAPPDATA%\imaginary
echo   2) %USERPROFILE%\Pictures\imaginary
echo   3) . (current directory)
echo   4) Custom path
echo.
set "DATA_CHOICE="
set /p "DATA_CHOICE=Choose [1-4, default=1]: "

if "!DATA_CHOICE!"=="2" (
    set "DATA_DIR=%USERPROFILE%\Pictures\imaginary"
) else if "!DATA_CHOICE!"=="3" (
    set "DATA_DIR=."
) else if "!DATA_CHOICE!"=="4" (
    set "CUSTOM_PATH="
    set /p "CUSTOM_PATH=Enter path: "
    if "!CUSTOM_PATH!"=="" (
        echo No path entered, using default.
        set "DATA_DIR=%LOCALAPPDATA%\imaginary"
    ) else (
        set "DATA_DIR=!CUSTOM_PATH!"
    )
) else (
    set "DATA_DIR=%LOCALAPPDATA%\imaginary"
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
echo   Imaginary Installer
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
echo --- Installing PyTorch (with CUDA support) ---
"%VENV_PIP%" install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124
if !errorlevel! neq 0 goto :error

echo.
echo --- Installing OpenCLIP ---
"%VENV_PIP%" install open_clip_torch
if !errorlevel! neq 0 goto :error

echo.
echo --- Installing face detection (facenet-pytorch) ---
"%VENV_PIP%" install --no-deps facenet-pytorch
if !errorlevel! neq 0 goto :error

echo.
echo --- Installing remaining dependencies ---
"%VENV_PIP%" install pillow opencv-python imagehash numpy pyyaml flask waitress orjson requests "transformers==4.44.*"
if !errorlevel! neq 0 goto :error

:: ---------------------------------------------------------------------------
:: Step 3/4: Initialise configuration
:: ---------------------------------------------------------------------------
echo.
echo ============================================================
echo   Step 3/4: Initialising configuration
echo ============================================================

if "!DATA_DIR_FLAG!"=="" (
    "%VENV_PYTHON%" app.py --list-models
) else (
    "%VENV_PYTHON%" app.py --data-dir "!DATA_DIR!" --list-models
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

:: GPU availability check
for /f "delims=" %%g in ('"%VENV_PYTHON%" -c "import torch; print('yes' if torch.cuda.is_available() else 'no')" 2^>nul') do set "CUDA_AVAILABLE=%%g"

if "!CUDA_AVAILABLE!"=="yes" (
    for /f "delims=" %%d in ('"%VENV_PYTHON%" -c "import torch; print(torch.cuda.get_device_name(0))" 2^>nul') do set "CUDA_DEVICE=%%d"
    echo   GPU: CUDA is available (!CUDA_DEVICE!).
) else (
    echo   GPU: CUDA is not available. Imaginary will use the CPU.
    echo        For GPU acceleration, install NVIDIA drivers and CUDA toolkit:
    echo        https://developer.nvidia.com/cuda-downloads
)

echo.
echo   To start Imaginary:
echo.
echo     %VENV_DIR%\Scripts\activate
if "!DATA_DIR_FLAG!"=="" (
    echo     python app.py
) else (
    echo     python app.py --data-dir "!DATA_DIR!"
)
echo.
echo   Then open http://localhost:5000 in your browser.
echo.

set "INSTALL_COMPLETE=1"
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
exit /b 1
