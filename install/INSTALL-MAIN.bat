@echo off
setlocal EnableExtensions DisableDelayedExpansion
title Kinoloom - main system installer
for %%I in ("%~dp0..") do set "DEF_DST=%%~fI"
rem a PYTHONPATH or PYTHONHOME from the PC would leak other packages into the venvs
set "PYTHONPATH="
set "PYTHONHOME="

rem INSTALL.bat calls this with /auto: mode [1] in this tree, every part's runtime
rem without the weights, and no questions beyond install\bootstrap.ps1's own
set "AUTO="
if /i "%~1"=="/auto" set "AUTO=1"
if not defined AUTO goto :menu
set "DST=%DEF_DST%"
set "RERUN=run INSTALL.bat again"
goto :scratch_go

:menu
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
set "RERUN=run this installer again with mode [3]"

if "%MODE%"=="1" goto :setup_scratch
if "%MODE%"=="2" goto :setup_mirror
if "%MODE%"=="3" goto :setup_parts
echo [FAIL] invalid choice "%MODE%"
goto :fail

rem ------------------------------------------------------------------
:setup_scratch
set "DST="
set /p DST="Studio root [%DEF_DST%]: "
if "%DST%"=="" set "DST=%DEF_DST%"
:scratch_go
if not exist "%DST%\tool\requirements.txt" echo [FAIL] %DST% is not a Kinoloom tree (tool\requirements.txt missing) & goto :fail
cd /d "%DST%"

call :step 1 "Python 3.12, Git and the Visual C++ runtime"
powershell -NoProfile -ExecutionPolicy Bypass -File "%DST%\install\bootstrap.ps1"
if errorlevel 1 goto :fail
rem tool\requirements.txt installs sd-scripts and SAM-2 straight from git; pip
rem starts git.exe itself, so a git.cmd wrapper on PATH does not count
where git.exe >nul 2>nul
if errorlevel 1 if exist "%DST%\tools\git\cmd\git.exe" set "PATH=%DST%\tools\git\cmd;%PATH%"
where git.exe >nul 2>nul
if errorlevel 1 echo [FAIL] git was not found (install\bootstrap.ps1 puts one in tools\git) & goto :fail

call :step 2 "the web app's Python environment, tool\.venv"
rem step 3 leaves this marker until pip has finished: a run that stopped there
rem starts the venv over (install_components.py does the same for its venvs)
if not exist "%DST%\tool\.venv\.kinoloom-incomplete" goto :venv_have
echo       tool\.venv was not finished last time; building it again.
"%DST%\tools\python\python.exe" -c "import sys; sys.path.insert(0, sys.argv[1]); from pathlib import Path; from install_components import remove_tree; remove_tree(Path(sys.argv[2]))" "%DST%\install" "%DST%\tool\.venv"
if exist "%DST%\tool\.venv\" echo [FAIL] could not remove tool\.venv - close Kinoloom if it is running, then run this again & goto :fail
:venv_have
if exist "%DST%\tool\.venv\Scripts\python.exe" goto :venv_ok
if not exist "%DST%\tools\python\python.exe" echo [FAIL] tools\python is missing (install\bootstrap.ps1 puts Python 3.12 there) & goto :fail
"%DST%\tools\python\python.exe" -m venv "%DST%\tool\.venv"
if errorlevel 1 echo [FAIL] could not create tool\.venv & goto :fail
:venv_ok
set "VENV_PY=%DST%\tool\.venv\Scripts\python.exe"

call :step 3 "the web app's packages, tool\requirements.txt"
echo       The longest step: pip downloads and installs about 190 packages,
echo       torch among them (a 2.6 GB download, from the cu126 index). Its
echo       bars show the progress; a line can stand still for a few minutes
echo       while a big package unpacks or builds.
echo pip has not finished this venv; the installer builds it again> "%DST%\tool\.venv\.kinoloom-incomplete"
"%VENV_PY%" -m pip install --upgrade pip
if errorlevel 1 echo [FAIL] pip upgrade failed & goto :fail
rem SAM-2's optional CUDA extension needs the CUDA toolkit and a C++ compiler; skip it
set "SAM2_BUILD_CUDA=0"
"%VENV_PY%" -m pip install -r "%DST%\tool\requirements.txt"
if errorlevel 1 (
  echo [FAIL] pip install failed - see the messages above. Run the installer
  echo        again once fixed: it builds tool\.venv again.
  goto :fail
)
del "%DST%\tool\.venv\.kinoloom-incomplete"

call :step 4 "folders and settings"
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

if defined AUTO call :step 5 "the render engines and their packages, no weights"
if not defined AUTO call :step 5 "the parts you choose"
if defined AUTO echo       Three ComfyUI engines with their add-ons, and two more Python
if defined AUTO echo       environments of about 10 GB together: several minutes. The
if defined AUTO echo       window title shows which item of the list it is on.
set "PARTS="
if defined AUTO set "PARTS=--components all --skip-models --yes"
set "PARTS_FAILED="
"%VENV_PY%" "%DST%\install\install_components.py" %PARTS%
if errorlevel 1 (
  set "PARTS_FAILED=1"
  echo   [WARN] some parts did not install - %RERUN%; finished steps are skipped.
)
set "FULL_VERIFY=0"
goto :verify

rem ------------------------------------------------------------------
:setup_parts
set "DST="
set /p DST="Studio root [%DEF_DST%]: "
if "%DST%"=="" set "DST=%DEF_DST%"
set "VENV_PY=%DST%\tool\.venv\Scripts\python.exe"
if not exist "%VENV_PY%" echo [FAIL] %DST%\tool\.venv is missing - run mode [1] first & goto :fail
cd /d "%DST%"
"%VENV_PY%" "%DST%\install\install_components.py"
if errorlevel 1 echo   [WARN] some parts did not install - run mode [3] again; finished steps are skipped.
set "FULL_VERIFY=0"
goto :verify

rem ------------------------------------------------------------------
:setup_mirror
set "SRC="
set /p SRC="Source Kinoloom tree (a path or a share, e.g. \\MAIN\Kinoloom): "
if "%SRC%"=="" echo [FAIL] a source path is required & goto :fail
if not exist "%SRC%\tool\launcher.py" echo [FAIL] %SRC% does not look like a Kinoloom tree (tool\launcher.py missing) & goto :fail
set "DST="
set /p DST="Install target [%DEF_DST%]: "
if "%DST%"=="" set "DST=%DEF_DST%"

echo.
echo [1/2] copying the tree (robocopy /E: never deletes; re-run to resume)...
robocopy "%SRC%" "%DST%" /E /XA:S /R:2 /W:5 /NFL /NDL /NP /XD temp output logs __pycache__
if errorlevel 8 echo [FAIL] robocopy failed - free disk space? share reachable? & goto :fail
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
title Kinoloom install - checking the install
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

rem INSTALL.bat prints its own summary
if defined AUTO if defined PARTS_FAILED exit /b 2
if defined AUTO exit /b 0

echo.
echo ==============================================================
echo  DONE. Start the studio with "%DST%\start.bat"
echo ==============================================================
pause
exit /b 0

:fail
if not defined AUTO pause
exit /b 1

rem ------------------------------------------------------------------
:step
rem %1 = the step, %2 = what it does. The window title repeats it, so the
rem step stays in view while pip's output scrolls past.
title Kinoloom install - step %~1 of 5: %~2 - keep this window open
echo.
echo [%~1/5] %time:~0,5%  %~2...
exit /b 0
