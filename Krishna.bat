@echo off
REM One entry point: starts the Streamlit server if it isn't already running,
REM then opens Krishna in its own window (see app_launcher.py for the ladder).
cd /d "%~dp0"
start "" venv\Scripts\pythonw.exe app_launcher.py
