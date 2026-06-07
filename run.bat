@echo off
REM Run the Viewer with Windows Python (dev mode, no packaging).
REM Uses pushd so it works even from a \\wsl$ UNC path (maps a temp drive).
pushd "%~dp0"
python cc_pulse_viewer.py
set RC=%errorlevel%
popd
if not "%RC%"=="0" pause
