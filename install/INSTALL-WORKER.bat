@echo off
setlocal EnableExtensions DisableDelayedExpansion
title Kinoloom - WORKER installer
rem Template: the studio serves this file from Workers -> Download installer
rem (tool/routers/fleet.py) with the double-underscore placeholders filled
rem in. The "was it filled in?" checks below spell each placeholder as
rem %_P%NAME%_P% so that the server's replace pass cannot rewrite them.
rem Every prompt can be pre-answered with an environment variable:
rem KINOLOOM_SRC, KINOLOOM_DST, KINOLOOM_WORKER_NAME, KINOLOOM_MAIN_URL,
rem KINOLOOM_WORKER_TOKEN, KINOLOOM_PACKAGES (menu number).
set "_P=__"
echo ==============================================================
echo  KINOLOOM - WORKER INSTALLER
echo.
echo  Sets up a worker PC: embedded Python + the shared env, the
echo  render-farm ComfyUI with its custom nodes, the model packages
echo  you pick, and the worker agent. The agent registers with the
echo  studio when it starts.
echo.
echo  You need a reachable copy of the golden tree (share the main
echo  studio's folder on the network first).
echo ==============================================================
set "_BW=__BIND_WARNING__"
if "%_BW%"=="%_P%BIND_WARNING%_P%" set "_BW="
if not "%_BW%"=="" echo %_BW%
echo.

set "SRC=%KINOLOOM_SRC%"
if not "%SRC%"=="" goto :src_done
rem run straight from the share: offer the tree this script lives in
if exist "%~dp0..\ComfyUI\main.py" for %%I in ("%~dp0..") do set "SRC=%%~fI"
if "%SRC%"=="" goto :src_ask
set /p SRC="Source Kinoloom tree [%SRC%]: "
goto :src_done
:src_ask
set /p SRC="Source Kinoloom tree (network path or drive, e.g. \\MAIN\Kinoloom): "
:src_done
if "%SRC%"=="" echo [FAIL] source required & pause & exit /b 1
if not exist "%SRC%\ComfyUI\main.py" echo [FAIL] %SRC% is not a Kinoloom tree & pause & exit /b 1
if not exist "%SRC%\install\copy_packages.ps1" echo [FAIL] %SRC% has no install\copy_packages.ps1 - update the main studio first & pause & exit /b 1

set "DST=%KINOLOOM_DST%"
if "%DST%"=="" set /p DST="Install root on this PC [C:\Kinoloom_worker]: "
if "%DST%"=="" set "DST=C:\Kinoloom_worker"
set "DSTY=%DST:\=/%"

set "WNAME=__WORKER_NAME__"
if "%WNAME%"=="%_P%WORKER_NAME%_P%" set "WNAME="
if not "%KINOLOOM_WORKER_NAME%"=="" set "WNAME=%KINOLOOM_WORKER_NAME%"
if "%WNAME%"=="" set /p WNAME="Worker name (shown in the studio) [%COMPUTERNAME%]: "
if "%WNAME%"=="" set "WNAME=%COMPUTERNAME%"

set "MURL=__MAIN_URL__"
if "%MURL%"=="%_P%MAIN_URL%_P%" set "MURL="
if not "%KINOLOOM_MAIN_URL%"=="" set "MURL=%KINOLOOM_MAIN_URL%"
if not "%MURL%"=="" goto :have_url
set /p MURL="Main studio URL, e.g. http://192.168.1.10:8080: "
goto :url_done
:have_url
rem blank input keeps the embedded URL
set /p MURL="Main studio URL [%MURL%]: "
:url_done
if "%MURL%"=="" echo [FAIL] main URL required & pause & exit /b 1

rem The studio refuses workers without the fleet token (workers.shared_token).
set "WTOKEN=__TOKEN__"
if "%WTOKEN%"=="%_P%TOKEN%_P%" set "WTOKEN="
if not "%KINOLOOM_WORKER_TOKEN%"=="" set "WTOKEN=%KINOLOOM_WORKER_TOKEN%"
if "%WTOKEN%"=="" set /p WTOKEN="Fleet token (studio Workers page): "
if "%WTOKEN%"=="" echo [FAIL] fleet token required - download this installer from the studio's Workers page & pause & exit /b 1

echo.
echo Pick model packages to copy (install\model_packages.json):
echo   [1] Image Studio          (image-sdxl, image-zimage, image-cyberrealistic)
echo   [2] Post / upscale lane   (post-upscale: RealESRGAN x4)
echo   [3] Image Studio + post   (both of the above)
echo   [4] Everything            (full mirror of models\ + external_loras\)
echo   [5] Skip models           (I will sync them myself)
echo.
echo   This installs the render-farm ComfyUI: image, Wan and post jobs.
echo   MiniMax H3 / LTX-2.5 graphs need the ComfyUI_LTX tree, which this
echo   installer does not set up - the studio refuses to send them to a
echo   worker whose ComfyUI lacks their nodes (docs\setup\DISTRIBUTED_FLEET.md).
set "PKG=%KINOLOOM_PACKAGES%"
if "%PKG%"=="" set /p PKG="Choice [1]: "
if "%PKG%"=="" set "PKG=1"
set "PKGS="
if "%PKG%"=="1" set "PKGS=image-sdxl,image-zimage,image-cyberrealistic"
if "%PKG%"=="2" set "PKGS=post-upscale"
if "%PKG%"=="3" set "PKGS=image-sdxl,image-zimage,image-cyberrealistic,post-upscale"
if "%PKG%"=="4" set "PKGS=*"
if "%PKG%"=="5" set "PKGS=-"
if "%PKGS%"=="" echo [FAIL] unknown choice "%PKG%" & pause & exit /b 1

echo.
echo   Studio URL  : %MURL%
echo   Worker name : %WNAME%
echo   Install to  : %DST%
echo   Packages    : %PKGS%
echo.
echo [1/6] embedded Python + shared env (identical bytes to the main PC)...
robocopy "%SRC%\tools\python" "%DST%\tools\python" /E /R:2 /W:5 /NFL /NDL /NP
if errorlevel 8 goto :fail
robocopy "%SRC%\tool\.venv" "%DST%\tool\.venv" /E /R:2 /W:5 /NFL /NDL /NP
if errorlevel 8 goto :fail

echo [2/6] ComfyUI (pinned version + custom nodes + its own models dir)...
robocopy "%SRC%\ComfyUI" "%DST%\ComfyUI" /E /R:2 /W:5 /NFL /NDL /NP /XD .git __pycache__
if errorlevel 8 goto :fail

echo [3/6] model packages...
if "%PKGS%"=="-" goto :models_done
if not "%PKGS%"=="*" goto :models_manifest
robocopy "%SRC%\models" "%DST%\models" /E /R:2 /W:5 /NFL /NDL /NP
if errorlevel 8 goto :fail
if not exist "%SRC%\external_loras" goto :models_done
robocopy "%SRC%\external_loras" "%DST%\external_loras" /E /R:2 /W:5 /NFL /NDL /NP
if errorlevel 8 goto :fail
goto :models_done
:models_manifest
rem robocopy file filters match bare NAMES, never dir\file paths, so the
rem manifest's files are copied one by one (it is the single source of truth).
powershell -NoProfile -ExecutionPolicy Bypass -File "%SRC%\install\copy_packages.ps1" -Source "%SRC%" -Dest "%DST%" -Packages "%PKGS%"
if errorlevel 1 goto :fail
:models_done

echo [4/6] worker agent...
robocopy "%SRC%\tool\worker" "%DST%\worker" /E /R:2 /W:5 /NFL /NDL /NP /XD __pycache__ /XF worker_config.yaml
if errorlevel 8 goto :fail

echo [5/6] configs...
(
  echo # generated by INSTALL-WORKER.bat - worker-side config
  echo name: "%WNAME%"
  echo main_url: "%MURL%"
  echo token: "%WTOKEN%"
  echo agent_port: 8120
  echo comfyui_port: 8188
  echo autostart_comfy: true
  echo heartbeat_s: 10
  echo comfyui_dir: "%DSTY%/ComfyUI"
  echo models_dir: "%DSTY%/models"
  echo python: "%DSTY%/tools/python/python.exe"
  echo.
  echo # --- worker roles: which jobs the studio may assign here.
  echo #     video = shot renders, image = Image Studio, upscale = post lane.
  echo roles: [video, image, upscale]
  echo.
  echo # --- optional start gating: with require_models true the agent reports
  echo #     itself blocked ^(and refuses jobs^) until every listed file exists
  echo #     under models_dir. Install them from the studio's Workers page
  echo #     ^("Install models"^) - nothing installs itself.
  echo require_models: false
  echo # required_models:
  echo #   - upscale_models/RealESRGAN_x4plus.pth
) > "%DST%\worker\worker_config.yaml"

(
  echo # worker-local model paths - points ComfyUI at THIS box's models dir.
  echo kinoloom_worker:
  echo   base_path: "%DSTY%/models"
  echo   checkpoints: checkpoints
  echo   diffusion_models: diffusion_models
  echo   text_encoders: text_encoders
  echo   vae: vae
  echo   loras: loras
  echo   clip: clip
  echo   clip_vision: clip_vision
  echo   ipadapter: ipadapter
  echo   upscale_models: upscale_models
  echo   latent_upscale_models: latent_upscale_models
) > "%DST%\ComfyUI\extra_model_paths.yaml"

(
  echo @echo off
  echo title Kinoloom Worker - %WNAME%
  echo set "PYTHONPATH=%DST%\tool\.venv\Lib\site-packages"
  echo cd /d "%DST%\worker"
  echo "%DST%\tools\python\python.exe" -m uvicorn worker_agent:app --host 0.0.0.0 --port 8120
  echo pause
) > "%DST%\WORKER.bat"

echo [6/6] done.
echo.
echo   Worker config : %DST%\worker\worker_config.yaml
echo   Launcher      : %DST%\WORKER.bat   (double-click to go online;
echo                   the agent starts its own ComfyUI and registers with
echo                   %MURL%)
echo   The worker appears in the studio's Workers page within seconds.
echo.
echo   NOTE: the render farm needs NVIDIA driver R560 or newer (CUDA 12.6).
pause
exit /b 0

:fail
echo [FAIL] a copy step failed - check free space / share reachability.
pause
exit /b 1
