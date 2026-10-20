@echo off
cd /d "%~dp0"
echo === Zoom Auto Recorder setup ===

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python not found. Install Python 3.10+ from https://www.python.org/ and check "Add python.exe to PATH".
  pause
  exit /b 1
)

echo [1/2] Installing Python packages...
python -m pip install --upgrade pip >nul
python -m pip install -r requirements.txt
if errorlevel 1 (
  echo [ERROR] pip install failed.
  pause
  exit /b 1
)

echo [2/2] Checking ffmpeg...
where ffmpeg >nul 2>nul
if errorlevel 1 (
  if exist "%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe" (
    echo ffmpeg already installed via winget.
  ) else (
    echo Installing ffmpeg with winget...
    winget install -e --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements
    if errorlevel 1 echo [WARN] Could not install ffmpeg automatically. Put ffmpeg.exe in this folder or set its path in the app.
  )
) else (
  echo ffmpeg found.
)

echo.
echo Setup finished. Start the app with run.bat
pause
