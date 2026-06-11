@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

echo ============================================================
echo   HybridMI-BCI GUI — Windows Build Script  v2.2.5
echo ============================================================
echo.

:: ── Configuration ──────────────────────────────────────────────
set "PROJECT_DIR=%~dp0"
set "VENV_DIR=%PROJECT_DIR%win_venv"
set "SPEC_FILE=%PROJECT_DIR%bci_win.spec"
set "REQ_FILE=%PROJECT_DIR%requirements_win.txt"
set "DIST_DIR=%PROJECT_DIR%dist"
set "APP_NAME=HybridMI_BCI"

:: ── Check Python ───────────────────────────────────────────────
where python >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python not found! Please install Python 3.10+
    echo         https://www.python.org/downloads/
    pause
    exit /b 1
)

for /f "tokens=2" %%V in ('python --version 2^>^&1') do set PYVER=%%V
for /f "tokens=*" %%A in ('python -c "import struct;print(struct.calcsize('P')*8)"') do set PYBITS=%%A
echo [INFO]  Python %PYVER% (%PYBITS%-bit^)
echo.

:: ── Determine target architecture ──────────────────────────────
set "TARGET_ARCH="
if not "%~1"=="" (
    if /i "%~1"=="32" set TARGET_ARCH=32
    if /i "%~1"=="64" set TARGET_ARCH=64
    if /i "%~1"=="x86" set TARGET_ARCH=32
    if /i "%~1"=="x64" set TARGET_ARCH=64
    echo [INFO]  Requested architecture: %TARGET_ARCH%-bit
) else (
    set TARGET_ARCH=%PYBITS%
    echo [INFO]  Building for current Python architecture: %PYBITS%-bit
)

if not "%TARGET_ARCH%"=="%PYBITS%" (
    echo [WARN]  Current Python is %PYBITS%-bit, but %TARGET_ARCH%-bit requested!
    echo        Install a %TARGET_ARCH%-bit Python and re-run from it.
    echo        e.g. py -%TARGET_ARCH% -m venv win_venv_%TARGET_ARCH%
    echo        Then: win_venv_%TARGET_ARCH%\Scripts\activate
    echo        Then: build_windows.bat
    pause
    exit /b 1
)

:: ── Check ICO ──────────────────────────────────────────────────
if not exist "%PROJECT_DIR%软件图标.ico" (
    echo [WARN]  Icon file not found: 软件图标.ico
    echo        .exe will be built without icon.
)

:: ── Check source ───────────────────────────────────────────────
if not exist "%PROJECT_DIR%src\main.py" (
    echo [ERROR] Source not found: src\main.py
    pause
    exit /b 1
)

:: ── Create virtual environment ─────────────────────────────────
echo.
echo [STEP 1/4] Creating virtual environment...
if exist "%VENV_DIR%" (
    echo          Removing old venv...
    rmdir /s /q "%VENV_DIR%"
)
python -m venv "%VENV_DIR%"
if %errorlevel% neq 0 (
    echo [ERROR] Failed to create venv
    pause
    exit /b 1
)

call "%VENV_DIR%\Scripts\activate.bat"
echo [ OK ]  Virtual environment ready

:: ── Install dependencies ───────────────────────────────────────
echo.
echo [STEP 2/4] Installing dependencies...
python -m pip install --upgrade pip -q
pip install -r "%REQ_FILE%"
if %errorlevel% neq 0 (
    echo [ERROR] Failed to install dependencies
    pause
    exit /b 1
)
echo [ OK ]  Dependencies installed

:: ── Build ──────────────────────────────────────────────────────
echo.
echo [STEP 3/4] Building %APP_NAME% (%TARGET_ARCH%-bit^)...
echo          This may take 3-5 minutes...

:: Clean previous build
if exist "%DIST_DIR%" rmdir /s /q "%DIST_DIR%"
if exist "%PROJECT_DIR%build" rmdir /s /q "%PROJECT_DIR%build"

pyinstaller "%SPEC_FILE%" --noconfirm --clean
if %errorlevel% neq 0 (
    echo [ERROR] PyInstaller build failed
    pause
    exit /b 1
)
echo [ OK ]  Build complete

:: ── Organize output ────────────────────────────────────────────
echo.
echo [STEP 4/4] Organizing output...

set "OUT_DIR=%PROJECT_DIR%release\HybridMI-BCI-v2.2.5-win%TARGET_ARCH%"
if exist "%OUT_DIR%" rmdir /s /q "%OUT_DIR%"
mkdir "%OUT_DIR%"

:: Copy dist files
xcopy /e /i /q "%DIST_DIR%\%APP_NAME%\*" "%OUT_DIR%\"
if %errorlevel% neq 0 (
    echo [WARN] xcopy may have partial failures
)

:: Rename exe for clarity
if exist "%OUT_DIR%\%APP_NAME%.exe" (
    ren "%OUT_DIR%\%APP_NAME%.exe" "HybridMI-BCI.exe"
)

:: Clean up build artifacts
rmdir /s /q "%PROJECT_DIR%build"

echo [ OK ]  Output: %OUT_DIR%

:: ── Summary ────────────────────────────────────────────────────
echo.
echo ============================================================
echo   BUILD SUCCESSFUL
echo ============================================================
echo.
echo   Architecture:  %TARGET_ARCH%-bit
echo   Output:        %OUT_DIR%
echo   Executable:    %OUT_DIR%\HybridMI-BCI.exe
echo.
echo   To build for the other architecture, install the
echo   matching Python and re-run this script from it.
echo ============================================================

:: Cleanup
call deactivate >nul 2>&1
endlocal

:: Keep window open
echo.
echo Press any key to exit...
pause >nul
