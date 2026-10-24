@echo off
setlocal EnableExtensions DisableDelayedExpansion
title Kinoloom - main system installer
echo ==============================================================
echo  KINOLOOM - MAIN SYSTEM INSTALLER
echo.
echo  [1] Install in this tree (a git clone): the control room, then the
echo      parts you choose - LTX-2.5, MiniMax H3, Qwen-Image, SDXL, post -
echo      with their ComfyUI trees, venvs, weights and ffmpeg.
echo  [2] Mirror a complete golden tree from another PC or a share
echo      (ComfyUI trees, venvs, models - everything).
echo  [3] Add parts to an installed studio, or check what is installed.
echo ==============================================================
echo.
set "MODE="
set /p MODE="Choice [1]: "
if "%MODE%"=="" set "MODE=1"
for %%I in ("%~dp0..") do set "DEF_DST=%%~fI"

if "%MODE%"=="1" goto :setup_scratch
if "%MODE%"=="2" goto :setup_mirror
if "%MODE%"=="3" goto :setup_parts
echo [FAIL] invalid choice "%MODE%"
pause
exit /b 1

rem ------------------------------------------------------------------
:setup_scratch
set "DST="
set /p DST="Studio root [%DEF_DST%]: "
if "%DST%"=="" set "DST=%DEF_DST%"
if not exist "%DST%\tool\requirements.txt" echo [FAIL] %DST% is not a Kinoloom tree (tool\requirements.txt missing) & pause & exit /b 1
cd /d "%DST%"

echo.
echo [1/5] Python 3.12 (64-bit)...
set "PY_EXE="
if exist "%DST%\tools\python\python.exe" set "PY_EXE=%DST%\tools\python\python.exe"
if defined PY_EXE goto :py_check
for /f "delims=" %%P in ('py -3.12 -c "import sys; print(sys.executable)" 2^>nul') do set "PY_EXE=%%P"
if defined PY_EXE goto :py_check
for /f "delims=" %%P in ('python -c "import sys; print(sys.executable)" 2^>nul') do set "PY_EXE=%%P"
if defined PY_EXE goto :py_check
echo [FAIL] Python 3.12 was not found (tools\python, the py launcher, or PATH).
echo        Install Python 3.12 64-bit from https://www.python.org/downloads/
echo        or run: winget install -e --id Python.Python.3.12
echo        - the pinned numpy/scipy wheels need exactly 3.12. Then run this again.
pause
exit /b 1
:py_check
"%PY_EXE%" -c "import sys; sys.exit(0 if sys.version_info[:2] == (3, 12) and sys.maxsize > 2**32 else 1)"
if not errorlevel 1 goto :py_ok
echo [FAIL] %PY_EXE% is not a 64-bit Python 3.12 - install 3.12 64-bit.
pause
exit /b 1
:py_ok
echo   using %PY_EXE%

rem tool\requirements.txt installs sd-scripts straight from git
where git >nul 2>nul
if not errorlevel 1 goto :git_ok
if exist "%DST%\tools\git\cmd\git.exe" set "PATH=%DST%\tools\git\cmd;%PATH%"
where git >nul 2>nul
if not errorlevel 1 goto :git_ok
echo [FAIL] git was not found - install Git for Windows (https://git-scm.com/)
echo        or run: winget install -e --id Git.Git - then run this again.
pause
exit /b 1
:git_ok

echo.
echo [2/5] virtual environment (tool\.venv)...
if exist "%DST%\tool\.venv\Scripts\python.exe" goto :venv_ok
"%PY_EXE%" -m venv "%DST%\tool\.venv"
if errorlevel 1 echo [FAIL] could not create tool\.venv & pause & exit /b 1
:venv_ok
set "VENV_PY=%DST%\tool\.venv\Scripts\python.exe"

echo.
echo [3/5] dependencies (tool\requirements.txt; torch from the cu126 index)...
"%VENV_PY%" -m pip install --upgrade pip
if errorlevel 1 echo [FAIL] pip upgrade failed & pause & exit /b 1
"%VENV_PY%" -m pip install -r "%DST%\tool\requirements.txt"
if errorlevel 1 (
  echo [FAIL] pip install failed - see the messages above. Re-run once fixed;
  echo        already-installed packages are kept.
  pause
  exit /b 1
)

echo.
echo [4/5] folders and settings...
for %%D in (temp logs output shots models external_loras\VideoGeneration) do (
  if not exist "%DST%\%%D" mkdir "%DST%\%%D"
)
if exist "%DST%\tool\settings.yaml" (
  echo   tool\settings.yaml exists - kept as is.
) else (
  copy "%DST%\tool\settings.example.yaml" "%DST%\tool\settings.yaml" >nul
  echo   tool\settings.yaml created from tool\settings.example.yaml.
)
rem the colour grade the post pass applies (post.lut in settings)
if not exist "%DST%\bible\luts\cinematic_teal_amber.cube" "%VENV_PY%" "%DST%\tool\lutgen.py"
rem the live-progress page ComfyUI serves from its frontend package (install\overlay\README.md)
set "FE_STATIC=%DST%\tool\.venv\Lib\site-packages\comfyui_frontend_package\static"
if exist "%FE_STATIC%\" copy /y "%DST%\install\overlay\progress_relay.html" "%FE_STATIC%\" >nul
if exist "%FE_STATIC%\progress_relay.html" echo   progress_relay.html deployed to the render farm's frontend package.

echo.
echo [5/5] the parts you choose (install\install_components.py)...
"%VENV_PY%" "%DST%\install\install_components.py"
if errorlevel 1 echo   [WARN] some parts did not install - run this installer again with mode [3]; finished steps are skipped.
set "FULL_VERIFY=0"
goto :verify

rem ------------------------------------------------------------------
:setup_parts
set "DST="
set /p DST="Studio root [%DEF_DST%]: "
if "%DST%"=="" set "DST=%DEF_DST%"
set "VENV_PY=%DST%\tool\.venv\Scripts\python.exe"
if not exist "%VENV_PY%" echo [FAIL] %DST%\tool\.venv is missing - run mode [1] first & pause & exit /b 1
cd /d "%DST%"
"%VENV_PY%" "%DST%\install\install_components.py"
if errorlevel 1 echo   [WARN] some parts did not install - run mode [3] again; finished steps are skipped.
set "FULL_VERIFY=0"
goto :verify

rem ------------------------------------------------------------------
:setup_mirror
set "SRC="
set /p SRC="Source Kinoloom tree (a path or a share, e.g. \\MAIN\Kinoloom): "
if "%SRC%"=="" echo [FAIL] a source path is required & pause & exit /b 1
if not exist "%SRC%\tool\launcher.py" echo [FAIL] %SRC% does not look like a Kinoloom tree (tool\launcher.py missing) & pause & exit /b 1
set "DST="
set /p DST="Install target [%DEF_DST%]: "
if "%DST%"=="" set "DST=%DEF_DST%"

echo.
echo [1/2] copying the tree (robocopy /E: never deletes; re-run to resume)...
robocopy "%SRC%" "%DST%" /E /XA:S /R:2 /W:5 /NFL /NDL /NP /XD temp output logs __pycache__
if errorlevel 8 echo [FAIL] robocopy failed - free disk space? share reachable? & pause & exit /b 1
cd /d "%DST%"

echo.
echo [2/2] folders...
for %%D in (temp logs output shots models external_loras\VideoGeneration) do (
  if not exist "%DST%\%%D" mkdir "%DST%\%%D"
)
echo   NOTE: tool\settings.yaml was copied too, including its API and fleet
echo         tokens. Delete those two values to have new ones generated.
set "FULL_VERIFY=1"
goto :verify

rem ------------------------------------------------------------------
:verify
echo.
echo [verify] install\verify_install.py
set "TEST_PY=python"
if exist "%DST%\tool\.venv\Scripts\python.exe" set "TEST_PY=%DST%\tool\.venv\Scripts\python.exe"
"%TEST_PY%" "%DST%\install\verify_install.py" --quick
if errorlevel 1 echo   [WARN] quick verify reported problems - review the list above
if "%FULL_VERIFY%"=="1" if exist "%DST%\tool\.venv\Scripts\python.exe" (
  echo.
  echo   full verification ^(sizes, hashes, required files^)...
  "%DST%\tool\.venv\Scripts\python.exe" "%DST%\install\verify_install.py"
)

echo.
echo [driver] NVIDIA driver...
nvidia-smi >nul 2>nul
if errorlevel 1 (
  echo   [WARN] nvidia-smi not found - install the NVIDIA driver.
) else (
  nvidia-smi --query-gpu=driver_version,name --format=csv,noheader
)
echo   Needed: R560 or newer for the CUDA 12.6 runtimes ^(studio, render farm,
echo   Qwen^), R580 or newer for the CUDA 13.0 LTX-2.5 / MiniMax H3 runtime.
echo   Tested on 616.64.

echo.
echo ==============================================================
echo  DONE. Start the studio with "%DST%\start.bat"
echo ==============================================================
pause
exit /b 0
