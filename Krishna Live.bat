@echo off
title Krishna Live
cd /d "%~dp0"
echo Starting Krishna Live (true barge-in - just talk to interrupt)...
start "" http://127.0.0.1:8611
"%~dp0venv\Scripts\python.exe" server.py
pause
