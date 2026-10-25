@echo off
rem Starts Live Tracker and opens it in the default browser. install.bat puts Live Tracker.bat on the desktop to run it.
rem The recognition service and the web UI run in the background, without windows; their messages go to
rem data\logs. This window closes once both answer. Stop the system, in Settings, stops them. What already runs
rem is left as it is, so starting Live Tracker again only opens the browser.
title Live Tracker
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
    echo Live Tracker is not installed in this folder yet: run install.bat first.
    pause
    exit /b 1
)
.venv\Scripts\python.exe -m live_tracker start
if errorlevel 1 pause
