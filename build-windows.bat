@echo off
setlocal
cd /d "%~dp0"

py -3.12 --version >nul 2>&1
if errorlevel 1 (
  echo Python 3.12 is required. Install it from python.org, then run this file again.
  pause
  exit /b 1
)

if not exist ".venv-windows\Scripts\python.exe" py -3.12 -m venv .venv-windows
if errorlevel 1 goto :failed

.venv-windows\Scripts\python.exe -m pip install --upgrade pip
if errorlevel 1 goto :failed
.venv-windows\Scripts\python.exe -m pip install -r requirements.txt "pyinstaller>=6.0"
if errorlevel 1 goto :failed
.venv-windows\Scripts\python.exe -c "import tkinter, cv2, mss, pyautogui, PIL"
if errorlevel 1 (
  echo Required Windows GUI libraries are unavailable. Reinstall Python 3.12 with Tcl/Tk enabled.
  goto :failed
)
.venv-windows\Scripts\python.exe -m unittest test_auto_fishing test_desktop_control tests.test_ui_redesign -v
if errorlevel 1 goto :failed

.venv-windows\Scripts\python.exe -m PyInstaller --noconfirm --clean --onefile --windowed --name RobloxAutoFishing-v0.0.5 --version-file windows-version-info.txt auto_fishing.py
if errorlevel 1 goto :failed

if exist "release\RobloxAutoFishing-v0.0.5" rmdir /s /q "release\RobloxAutoFishing-v0.0.5"
if errorlevel 1 goto :failed
mkdir "release\RobloxAutoFishing-v0.0.5"
if errorlevel 1 goto :failed
copy /y "dist\RobloxAutoFishing-v0.0.5.exe" "release\RobloxAutoFishing-v0.0.5\" >nul
if errorlevel 1 goto :failed
copy /y "WINDOWS-README.txt" "release\RobloxAutoFishing-v0.0.5\" >nul
if errorlevel 1 goto :failed
powershell -NoProfile -Command "Compress-Archive -Force -Path 'release\RobloxAutoFishing-v0.0.5\*' -DestinationPath 'release\RobloxAutoFishing-v0.0.5-Windows.zip'"
if errorlevel 1 goto :failed

echo.
echo Build complete: release\RobloxAutoFishing-v0.0.5-Windows.zip
pause
exit /b 0

:failed
echo.
echo Build failed. Read the error above.
pause
exit /b 1
