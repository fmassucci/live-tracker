@echo off
rem Installs Portaria in this folder: a Python environment in .venv with Portaria and its libraries, config.toml
rem and the face models. Then puts Portaria.bat on the desktop, which starts it. Run it again after an update:
rem it keeps config.toml, the models and data\.
setlocal
set "HERE=%~dp0"
cd /d "%HERE%"

if not exist pyproject.toml (
    echo Run install.bat from the Portaria folder. If it came as a .zip file, extract the whole folder first.
    goto failed
)

echo "%HERE%" | findstr /i "onedrive" >nul && (
    echo This folder is in OneDrive, which would copy data\, with its face photos, to the cloud.
    echo Move the folder out of OneDrive first, for example to C:\Portaria. To install here anyway, press a key.
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

echo Installing Portaria and its libraries. The first time, this takes a few minutes.
.venv\Scripts\python.exe -m pip install --disable-pip-version-check -e . || goto failed

if not exist config.toml (
    copy config.example.toml config.toml >nul || goto failed
    echo Created config.toml from config.example.toml
)

echo Downloading the face models that are missing
.venv\Scripts\python.exe -m live_tracker download-models || goto failed

rem Portaria.bat on the desktop runs start.bat from this folder
set "DESKTOP="
for /f "usebackq delims=" %%d in (`powershell -NoProfile -Command "[Environment]::GetFolderPath('Desktop')"`) do set "DESKTOP=%%d"
if not defined DESKTOP set "DESKTOP=%USERPROFILE%\Desktop"
set "LAUNCHER=%DESKTOP%\Portaria.bat"
(
    echo @echo off
    echo rem Made by install.bat: starts Portaria from its folder. If the folder moves, run install.bat there again.
    echo "%HERE%start.bat"
) > "%LAUNCHER%" || goto failed
echo Created "%LAUNCHER%"

echo.
echo Portaria is installed. To start it, double-click Portaria on the desktop.
echo Then sign in with user admin and password admin, and change that password in Settings.
pause
exit /b 0

:failed
echo.
echo The installation stopped because of the error above. Run install.bat again once it is fixed.
pause
exit /b 1
