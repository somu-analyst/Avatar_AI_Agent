@echo off
title Krishna Live
cd /d "%~dp0"
rem Kokoro's G2P reads a data file without declaring an encoding; without
rem this, Windows defaults to cp1252 and every sentence fails to speak.
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
echo Starting Krishna Live (true barge-in - just talk to interrupt)...
start "" http://127.0.0.1:8611
"%~dp0venv\Scripts\python.exe" -u server.py
pause
