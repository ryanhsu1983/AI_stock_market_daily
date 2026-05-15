@echo off
setlocal
cd /d "%~dp0"

set HTTP_PROXY=
set HTTPS_PROXY=
set ALL_PROXY=
set GIT_HTTP_PROXY=
set GIT_HTTPS_PROXY=

set "PYTHON=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not exist "%PYTHON%" set "PYTHON=python"

if exist "%~dp0email_preview.html" del "%~dp0email_preview.html"

echo Running stock signal preview...
"%PYTHON%" stock_market_tracking_system.py
if errorlevel 1 (
    echo.
    echo Preview failed. Review the error above.
    pause
    exit /b 1
)

if not exist "%~dp0email_preview.html" (
    echo.
    echo Preview was not created. Review the messages above.
    pause
    exit /b 1
)

echo.
echo Opening email_preview.html...
start "" "%~dp0email_preview.html"
exit /b 0
