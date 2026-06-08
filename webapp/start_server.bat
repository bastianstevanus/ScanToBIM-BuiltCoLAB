@echo off
REM ─────────────────────────────────────────────────────────────────────────
REM  Scan-to-BIM Registration Server
REM  Run this bat file to start the server in its own console window,
REM  independent of VS Code (crashes in VS Code won't kill the server).
REM ─────────────────────────────────────────────────────────────────────────
cd /d "%~dp0"
echo Starting Scan-to-BIM server at http://127.0.0.1:8000 ...
start "Scan-to-BIM Server" cmd /k "..\\.venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8000"
echo Server window opened. Press any key to close this launcher.
pause >nul
