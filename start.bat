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
if exist "%~dp0tools\python\python.exe" (
    set "PY=%~dp0tools\python\python.exe"
) else if exist "%~dp0tool\.venv\Scripts\python.exe" (
    set "PY=%~dp0tool\.venv\Scripts\python.exe"
) else (
    set "PY=python"
)
"%PY%" "%~dp0tool\launcher.py" %*
pause
