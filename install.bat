@echo off
rem Installs Live Tracker in this folder: a Python environment in .venv with Live Tracker and its libraries,
rem config.toml and the face models. Then puts a Live Tracker shortcut on the desktop, which starts it. Run it
rem again after an update: it keeps config.toml, the models and data\.
setlocal
set "HERE=%~dp0"
cd /d "%HERE%"

if not exist pyproject.toml (
    echo Run install.bat from the Live Tracker folder. If it came as a .zip file, extract the whole folder first.
    goto failed
)

echo "%HERE%" | findstr /i "onedrive" >nul && (
    echo This folder is in OneDrive, which would copy data\, with its face photos, to the cloud.
    echo Move the folder out of OneDrive first, for example to C:\live-tracker. To install here anyway, press a key.
    pause >nul
)

rem Python 3.11 to 3.14, 64-bit: the versions onnxruntime has Windows builds for. 3.13 is the one tested.
rem Looking for it never installs a Python and never waits for an answer.
set "SUPPORTED=import sys; sys.exit(not ((3, 11) <= sys.version_info[:2] <= (3, 14) and sys.maxsize > 2**32))"
set "PYTHON_MANAGER_AUTOMATIC_INSTALL=false"
set "PYTHON="
for %%p in ("py -3.13" "py -3.14" "py -3.12" "py -3.11" "python") do if not defined PYTHON (
    %%~p -c "%SUPPORTED%" <nul >nul 2>&1 && set "PYTHON=%%~p"
)
if not defined PYTHON (
    echo Python 3.13, 64-bit, was not found. Install it with this command, then run install.bat again:
    echo     winget install Python.Python.3.13
    echo Or with the Python install manager from https://www.python.org/downloads/ and: py install 3.13
    goto failed
)
%PYTHON% --version

rem A new environment, or a fresh one when the one there no longer works or has another Python version
.venv\Scripts\python.exe -c "%SUPPORTED%" <nul >nul 2>&1 || (
    echo Creating the Python environment in .venv
    %PYTHON% -m venv --clear --prompt live-tracker .venv || goto failed
)

echo Installing Live Tracker and its libraries. The first time, this takes a few minutes.
.venv\Scripts\python.exe -m pip install --disable-pip-version-check -e . || goto failed
rem Installed before under the app's old name, portaria: that package goes, and so does its desktop shortcut below
.venv\Scripts\python.exe -m pip uninstall --disable-pip-version-check -y -q portaria >nul 2>&1

if not exist config.toml (
    copy config.example.toml config.toml >nul || goto failed
    echo Created config.toml from config.example.toml
)

echo Downloading the face models that are missing
.venv\Scripts\python.exe -m live_tracker download-models || goto failed

rem The Live Tracker shortcut on the desktop runs start.bat from this folder, with the app's icon: a .bat file
rem can't have one. If the folder moves, running install.bat there again points the shortcut at it.
set "DESKTOP="
for /f "usebackq delims=" %%d in (`powershell -NoProfile -Command "[Environment]::GetFolderPath('Desktop')"`) do set "DESKTOP=%%d"
if not defined DESKTOP set "DESKTOP=%USERPROFILE%\Desktop"
if exist "%DESKTOP%\Portaria.bat" del "%DESKTOP%\Portaria.bat"
set "LAUNCHER=%DESKTOP%\Live Tracker.lnk"
set "ICON=%HERE%src\live_tracker\web\static\icon.ico"
powershell -NoProfile -Command "$ErrorActionPreference = 'Stop'; $s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:LAUNCHER); $s.TargetPath = $env:HERE + 'start.bat'; $s.WorkingDirectory = $env:HERE; $s.IconLocation = $env:ICON; $s.Save()" || goto failed
rem Installs before this one put Live Tracker.bat there instead
if exist "%DESKTOP%\Live Tracker.bat" del "%DESKTOP%\Live Tracker.bat"
echo Created "%LAUNCHER%"

echo.
echo Live Tracker is installed. To start it, double-click Live Tracker on the desktop.
echo Then sign in with user admin and password admin, and change that password in Settings.
pause
exit /b 0

:failed
echo.
echo The installation stopped because of the error above. Run install.bat again once it is fixed.
pause
exit /b 1
