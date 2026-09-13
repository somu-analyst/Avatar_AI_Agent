@echo off
REM Krishna launcher. PYTHONUTF8=1 works around a real bug in the installed
REM kokoro package: it opens its config JSON without specifying an encoding,
REM so Windows' default cp1252 locale crashes on the file's UTF-8 bytes.
REM This must be set before Python starts -- setting it from inside the
REM running script is too late, the interpreter's already initialized.
cd /d "%~dp0"
set PYTHONUTF8=1
venv\Scripts\python.exe krishna.py
pause
