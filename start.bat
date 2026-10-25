@echo off
rem ============================================================
rem  KINOLOOM - one window, everything inside
rem  Ctrl+C or closing this window stops ALL components.
rem  Extra model roots (e.g. H3 / LTX weight folders on another
rem  drive) come from tool\settings.yaml paths.extra_model_dirs.
rem ============================================================
title Kinoloom
setlocal
cd /d "%~dp0"
if exist "%~dp0tool\.venv\Scripts\python.exe" goto :installed
echo.
echo  Kinoloom is not installed on this PC yet. INSTALL.bat sets up
echo  everything it needs except the model weights.
echo.
set "GO="
set /p GO="Run INSTALL.bat now? [Y/n] "
if /i "%GO%"=="n" exit /b 1
if /i "%GO%"=="no" exit /b 1
call "%~dp0INSTALL.bat" /fromstart
if not exist "%~dp0tool\.venv\Scripts\python.exe" exit /b 1
title Kinoloom
:installed
if exist "%~dp0tools\python\python.exe" (
    set "PY=%~dp0tools\python\python.exe"
) else (
    set "PY=%~dp0tool\.venv\Scripts\python.exe"
)
"%PY%" "%~dp0tool\launcher.py" %*
pause
