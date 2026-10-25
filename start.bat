@echo off
rem Starts Portaria and opens it in the default browser. install.bat puts Portaria.bat on the desktop to run it.
rem The recognition service gets its own minimized window and the web UI runs in this one. What already runs is
rem left as it is, so starting Portaria again only opens the browser.
title Portaria - web UI
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
    echo Portaria is not installed in this folder yet: run install.bat first.
    pause
    exit /b 1
)
start "Portaria - recognition service" /min cmd /c ".venv\Scripts\python.exe -m portaria run || pause"
echo Closing this window stops the web UI. The recognition service keeps recording in its own window.
.venv\Scripts\python.exe -m portaria web --open
if errorlevel 1 pause
