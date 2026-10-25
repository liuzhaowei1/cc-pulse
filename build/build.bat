@echo off
REM ============================================================
REM CC Pulse Viewer - PyInstaller packaging (run on Windows)
REM Output: dist\CCPulse.exe  (--onefile --windowed, target <=30MB)
REM pushd handles \\wsl$ UNC paths by mapping a temp drive.
REM ============================================================
setlocal
pushd "%~dp0.."

echo [1/3] Checking PyInstaller...
python -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
  echo     Not found, installing pyinstaller ...
  python -m pip install --upgrade pyinstaller
)

echo [2/3] Building (onefile + windowed + bundled assets)...
python -m PyInstaller ^
  --onefile ^
  --windowed ^
  --name CCPulse ^
  --add-data "assets;assets" ^
  --icon "assets/app.ico" ^
  --noconfirm ^
  --clean ^
  cc_pulse_viewer.py

echo [3/3] Done. Artifact:
for %%F in (dist\CCPulse.exe) do echo     %%~fF  (%%~zF bytes)
echo.
echo Size should be ^<= 30MB. Double-click dist\CCPulse.exe to run.
popd
endlocal
pause
