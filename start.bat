@echo off
title OpenShorts Launcher
echo ========================================
echo    OpenShorts - Starting...
echo ========================================
echo.

:: Start Backend in a new window
echo [1/2] Starting Backend (FastAPI on port 8000)...
start "OpenShorts Backend" /D "%~dp0" cmd /k ".\venv\Scripts\uvicorn app:app --host 127.0.0.1 --port 8000"

:: Wait a moment for backend to initialize
timeout /t 3 /nobreak >nul

:: Start Frontend in a new window
echo [2/2] Starting Frontend (Vite on port 5173)...
start "OpenShorts Frontend" /D "%~dp0dashboard" cmd /k "npm run dev"

:: Wait a moment then open browser
timeout /t 3 /nobreak >nul
echo.
echo ========================================
echo    OpenShorts is running!
echo    http://localhost:5173
echo ========================================
echo.
echo Opening browser...
start http://localhost:5173

echo.
echo Close this window anytime - Backend and Frontend will keep running.
echo To stop: close the Backend and Frontend windows.
pause
