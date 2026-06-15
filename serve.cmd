@echo off
title Deep Research Viewer (http://127.0.0.1:8765)
cd /d "%~dp0"
".venv\Scripts\python.exe" -m web
echo.
echo [server stopped] press any key to close
pause >nul
