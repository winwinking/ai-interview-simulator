@echo off
title AI Interview Simulator
set "HERE=%~dp0"
if "%HERE:~-1%"=="\" set "HERE=%HERE:~0,-1%"
cd /d "%HERE%"

curl -s -o nul --max-time 2 http://127.0.0.1:5000/ 2>nul
if not errorlevel 1 goto open

echo Starting interview simulator server...
echo First launch builds the ChromaDB vector store (~1-2 min). Later launches load it in ~30s.
echo The browser will open automatically. Please keep this window minimized, do not close it.
start "AI-Interview-Server" /d "%HERE%" /min python app.py

set /a tries=0
:wait
timeout /t 2 /nobreak >nul
set /a tries+=1
curl -s -o nul --max-time 2 http://127.0.0.1:5000/ 2>nul
if not errorlevel 1 goto open
if %tries% geq 120 (
    echo.
    echo Timed out. Run "python app.py" in this folder to see the error.
    pause
    exit /b 1
)
goto wait

:open
start "" "http://127.0.0.1:5000/"
exit /b
