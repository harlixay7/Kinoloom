@echo off
setlocal EnableExtensions DisableDelayedExpansion
title Kinoloom - main system installer
for %%I in ("%~dp0..") do set "DEF_DST=%%~fI"
rem a PYTHONPATH or PYTHONHOME from the PC would leak other packages into the venvs
set "PYTHONPATH="
set "PYTHONHOME="
rem Every run logs to logs\install\install.log; a failure is also saved as
rem logs\install\LAST-FAILURE.txt, which start.bat shows (see :log_begin, :fail)
set "LOGDIR="
set "STEP=the start"
set "FAILMSG="
set "RC=1"
set "PS=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%PS%" set "PS=powershell"

rem INSTALL.bat calls this with /auto: mode [1] in this tree, every part's runtime
rem without the weights, and no questions beyond install\bootstrap.ps1's own
set "AUTO="
if /i "%~1"=="/auto" set "AUTO=1"
if not defined AUTO goto :menu
set "MODE=auto"
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
if defined MODE set "MODE=%MODE:"=%"
if not defined MODE set "MODE=1"
set "RERUN=run this installer again with mode [3]"

if "%MODE%"=="1" goto :setup_scratch
if "%MODE%"=="2" goto :setup_mirror
if "%MODE%"=="3" goto :setup_parts
set "FAILMSG=invalid choice %MODE%"
goto :fail

rem ------------------------------------------------------------------
:setup_scratch
set "DST="
set /p DST="Studio root [%DEF_DST%]: "
rem a typed or pasted path can come in quotes ("Copy as path"): they go
if defined DST set "DST=%DST:"=%"
if not defined DST set "DST=%DEF_DST%"
:scratch_go
if not exist "%DST%\tool\requirements.txt" set "FAILMSG=%DST% is not a Kinoloom tree (tool\requirements.txt missing)" & goto :fail
cd /d "%DST%" 2>nul || goto :no_cd
call :log_begin

call :step 1 "Python 3.12, Git and the Visual C++ runtime"
"%PS%" -NoProfile -ExecutionPolicy Bypass -File "%DST%\install\bootstrap.ps1"
set "RC=%errorlevel%"
if "%RC%"=="0" goto :bootstrap_ok
rem bootstrap.ps1 saves a report whenever it stops; without one, Windows
rem PowerShell did not run it to the end
if defined LOGDIR if exist "%LOGDIR%\LAST-FAILURE.txt" goto :fail
call :powershell_why
goto :fail
:bootstrap_ok
rem tool\requirements.txt installs sd-scripts and SAM-2 straight from git; pip
rem starts git.exe itself, so a git.cmd wrapper on PATH does not count
where git.exe >nul 2>nul
if errorlevel 1 if exist "%DST%\tools\git\cmd\git.exe" set "PATH=%DST%\tools\git\cmd;%PATH%"
where git.exe >nul 2>nul
if errorlevel 1 set "FAILMSG=git was not found (install\bootstrap.ps1 puts one in tools\git)" & goto :fail

call :step 2 "the web app's Python environment, tool\.venv"
set "VENV=%DST%\tool\.venv"
rem A venv is unfinished while it holds .kinoloom-incomplete: written before the
rem venv is created, deleted by install_support.py in step 3 once pip and the
rem import check went well. A venv a run left unfinished is made again, and so
rem is a venv folder without its python (a removal that stopped half-way) and a
rem venv whose python no longer starts (made in another folder: the Kinoloom
rem folder was moved). install_components.py does the same for its venvs.
set "REDO=tool\.venv was not finished last time; building it again."
if exist "%VENV%\.kinoloom-incomplete" goto :venv_redo
if not exist "%VENV%\Scripts\python.exe" goto :venv_nopy
"%VENV%\Scripts\python.exe" -c "" >nul 2>nul
if "%errorlevel%"=="0" goto :venv_ok
set "REDO=tool\.venv no longer starts (was the Kinoloom folder moved?); building it again."
goto :venv_redo
:venv_nopy
if not exist "%VENV%\" goto :venv_new
:venv_redo
echo       %REDO%
if defined LOGDIR >>"%LOGDIR%\install.log" echo   %REDO%
if not exist "%DST%\tools\python\python.exe" set "FAILMSG=tools\python is missing (install\bootstrap.ps1 puts Python 3.12 there)" & goto :fail
"%DST%\tools\python\python.exe" "%DST%\install\install_support.py" remove-venv "%VENV%"
if exist "%VENV%\" set "FAILMSG=could not remove tool\.venv - close Kinoloom if it is running" & goto :fail
:venv_new
if not exist "%DST%\tools\python\python.exe" set "FAILMSG=tools\python is missing (install\bootstrap.ps1 puts Python 3.12 there)" & goto :fail
mkdir "%VENV%"
echo pip has not finished this venv; the installer builds it again> "%VENV%\.kinoloom-incomplete"
"%DST%\tools\python\python.exe" -m venv "%VENV%"
set "RC=%errorlevel%"
if not "%RC%"=="0" set "FAILMSG=could not create tool\.venv (exit code %RC%)" & goto :fail
if defined LOGDIR >>"%LOGDIR%\install.log" echo   tool\.venv created from tools\python
:venv_ok
set "VENV_PY=%VENV%\Scripts\python.exe"

call :step 3 "the web app's packages, tool\requirements.txt"
echo       The longest step: pip downloads and installs about 190 packages,
echo       torch among them (a 2.6 GB download, from the cu126 index). Its
echo       bars show the progress; a line can stand still for a few minutes
echo       while a big package unpacks or builds. A venv finished from the
echo       same list before is only checked.
rem SAM-2's optional CUDA extension needs the CUDA toolkit and a C++ compiler; skip it
set "SAM2_BUILD_CUDA=0"
rem install_support.py: pip pinned, then the list (SAM-2 built after torch), one
rem more try after a network error, then the web app's packages must
rem import; with --venv it keeps the marker until all of that went well. pip
rem logs to logs\install\pip-tool-venv.log; a failure is printed and saved by
rem the helper itself (exit code 1).
"%VENV_PY%" "%DST%\install\install_support.py" venv tool-venv "%DST%\tool\requirements.txt" --venv "%VENV%" --check-import uvicorn,fastapi,yaml,psutil,numpy,PIL,cv2,torch --step "step 3 of 5, the web app's packages"
set "RC=%errorlevel%"
if not "%RC%"=="0" goto :fail

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
if not exist "%DST%\tool\settings.yaml" set "FAILMSG=could not create tool\settings.yaml from tool\settings.example.yaml" & goto :fail
if defined LOGDIR >>"%LOGDIR%\install.log" echo   tool\settings.yaml is there
rem the colour grade the post pass applies (post.lut in settings)
set "LUT=%DST%\bible\luts\cinematic_teal_amber.cube"
if not exist "%LUT%" "%VENV_PY%" "%DST%\tool\lutgen.py"
if not exist "%LUT%" echo   [WARN] tool\lutgen.py did not write bible\luts\cinematic_teal_amber.cube: the post pass has no colour grade until it does.
if defined LOGDIR if not exist "%LUT%" >>"%LOGDIR%\install.log" echo   [WARN] tool\lutgen.py did not write bible\luts\cinematic_teal_amber.cube
rem the live-progress page ComfyUI serves from its frontend package (install\overlay\README.md)
set "FE_STATIC=%DST%\tool\.venv\Lib\site-packages\comfyui_frontend_package\static"
if exist "%FE_STATIC%\" copy /y "%DST%\install\overlay\progress_relay.html" "%FE_STATIC%\" >nul
if exist "%FE_STATIC%\progress_relay.html" echo   progress_relay.html deployed to the render farm's frontend package.
if defined LOGDIR if exist "%FE_STATIC%\progress_relay.html" >>"%LOGDIR%\install.log" echo   progress_relay.html deployed

if defined AUTO call :step 5 "the render engines and their packages, no weights"
if not defined AUTO call :step 5 "the parts you choose"
if defined AUTO echo       Three ComfyUI engines with their add-ons, and two more Python
if defined AUTO echo       environments of about 9 GB together: several minutes. The
if defined AUTO echo       window title shows which item of the list it is on.
set "PARTS="
if defined AUTO set "PARTS=--components all --skip-models --yes"
set "PARTS_FAILED="
"%VENV_PY%" "%DST%\install\install_components.py" %PARTS%
set "RC=%errorlevel%"
if not "%RC%"=="0" set "PARTS_FAILED=1"
rem install_components.py reports its own failures; one it did not report gets one here
if defined PARTS_FAILED call :report "SOME PARTS DID NOT INSTALL"
if defined PARTS_FAILED echo   [WARN] some parts did not install - %RERUN%; finished steps are skipped.
set "FULL_VERIFY=0"
goto :verify

rem ------------------------------------------------------------------
:setup_parts
set "DST="
set /p DST="Studio root [%DEF_DST%]: "
rem a typed or pasted path can come in quotes ("Copy as path"): they go
if defined DST set "DST=%DST:"=%"
if not defined DST set "DST=%DEF_DST%"
set "VENV_PY=%DST%\tool\.venv\Scripts\python.exe"
if not exist "%VENV_PY%" set "FAILMSG=%DST%\tool\.venv is missing - run mode [1] first" & goto :fail
cd /d "%DST%" 2>nul || goto :no_cd
call :log_begin
set "STEP=the parts you choose"
set "PARTS_FAILED="
"%VENV_PY%" "%DST%\install\install_components.py"
set "RC=%errorlevel%"
if not "%RC%"=="0" set "PARTS_FAILED=1"
if defined PARTS_FAILED call :report "SOME PARTS DID NOT INSTALL"
if defined PARTS_FAILED echo   [WARN] some parts did not install - run mode [3] again; finished steps are skipped.
set "FULL_VERIFY=0"
goto :verify

rem ------------------------------------------------------------------
:setup_mirror
set "SRC="
set /p SRC="Source Kinoloom tree (a path or a share, e.g. \\MAIN\Kinoloom): "
if defined SRC set "SRC=%SRC:"=%"
if not defined SRC set "FAILMSG=a source path is required" & goto :fail
if not exist "%SRC%\tool\launcher.py" set "FAILMSG=%SRC% does not look like a Kinoloom tree (tool\launcher.py missing)" & goto :fail
set "DST="
set /p DST="Install target [%DEF_DST%]: "
if defined DST set "DST=%DST:"=%"
if not defined DST set "DST=%DEF_DST%"

echo.
echo [1/2] copying the tree (robocopy /E: never deletes; re-run to resume)...
set "STEP=copying the tree"
robocopy "%SRC%" "%DST%" /E /XA:S /R:2 /W:5 /NFL /NDL /NP /XD temp output logs __pycache__
if errorlevel 8 set "FAILMSG=robocopy failed - free disk space? share reachable?" & goto :fail
cd /d "%DST%" 2>nul || goto :no_cd
call :log_begin

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
rem with --log it also appends what it prints to install.log
set "VLOG="
if defined LOGDIR set VLOG=--log "%LOGDIR%\install.log"
if defined LOGDIR >>"%LOGDIR%\install.log" echo [verify] %time:~0,8%  install\verify_install.py --quick
"%TEST_PY%" "%DST%\install\verify_install.py" --quick %VLOG%
set "RC=%errorlevel%"
if not "%RC%"=="0" echo   [WARN] quick verify reported problems - review the list above
if defined LOGDIR >>"%LOGDIR%\install.log" echo [verify] quick verify exit code %RC%
if not "%FULL_VERIFY%"=="1" goto :verify_done
if not exist "%DST%\tool\.venv\Scripts\python.exe" goto :verify_done
echo.
echo   full verification (sizes, hashes, required files)...
"%DST%\tool\.venv\Scripts\python.exe" "%DST%\install\verify_install.py" %VLOG%
set "RC=%errorlevel%"
if defined LOGDIR >>"%LOGDIR%\install.log" echo [verify] full verify exit code %RC%
:verify_done

echo.
echo [driver] NVIDIA driver...
nvidia-smi >nul 2>nul
if errorlevel 1 (
  echo   [WARN] nvidia-smi not found - install the NVIDIA driver.
  if defined LOGDIR >>"%LOGDIR%\install.log" echo [driver] nvidia-smi not found
) else (
  nvidia-smi --query-gpu=driver_version,name --format=csv,noheader
  if defined LOGDIR >>"%LOGDIR%\install.log" echo [driver] NVIDIA driver version, GPU:
  if defined LOGDIR >>"%LOGDIR%\install.log" nvidia-smi --query-gpu=driver_version,name --format=csv,noheader
)
echo   Needed: R560 or newer and a GTX 900- to RTX 40-series card for the
echo   CUDA 12.6 runtimes ^(studio, render farm^); R580 or newer and a GTX
echo   16- or RTX 20-series card or newer for the CUDA 13.0 runtimes ^(LTX-2.5
echo   / MiniMax H3^). Qwen-Image 2.1 gets the build this card runs. Tested
echo   on 616.64.
rem what this PC's card and driver run (tool\gpu_support.py, standard library only)
set "GPU_PY="
if exist "%DST%\tools\python\python.exe" set "GPU_PY=%DST%\tools\python\python.exe"
if exist "%DST%\tool\.venv\Scripts\python.exe" set "GPU_PY=%DST%\tool\.venv\Scripts\python.exe"
set "GLOG="
if defined LOGDIR set GLOG=--log "%LOGDIR%\install.log"
if defined GPU_PY if exist "%DST%\tool\gpu_support.py" "%GPU_PY%" "%DST%\tool\gpu_support.py" %GLOG%

if defined PARTS_FAILED goto :done_partial
rem a finished install keeps none of pip's downloads (temp\pip-cache, temp\pip-tmp)
if defined VENV_PY if exist "%VENV_PY%" "%VENV_PY%" "%DST%\install\install_support.py" finish
if defined LOGDIR >>"%LOGDIR%\install.log" echo ==== finished %DATE% %TIME%: OK ====
rem INSTALL.bat prints its own summary
if defined AUTO set "QRC=0" & goto :quit
goto :done

:done_partial
rem pip's downloads stay, so the next run does not fetch them again
if defined LOGDIR >>"%LOGDIR%\install.log" echo ==== finished %DATE% %TIME%: some parts did not install ====
if defined AUTO set "QRC=2" & goto :quit
echo.
echo ==============================================================
echo  NOT FINISHED: some parts did not install - the report above says
echo  which and why. The parts that did install work; %RERUN%
echo  to finish the others (what is done is skipped).
echo ==============================================================
pause
set "QRC=2"
goto :quit

:done
echo.
echo ==============================================================
echo  DONE. Start the studio with "%DST%\start.bat"
echo ==============================================================
pause
set "QRC=0"
goto :quit

rem ------------------------------------------------------------------
:no_cd
rem every step runs in the studio folder; cmd can't make a network folder
rem (\\server\share) its current one
set "FAILMSG=could not open %DST% as the current folder. A network folder (\\server\share) does not work; use a folder on this PC, like C:\Kinoloom"
goto :fail

:powershell_why
rem Windows PowerShell missing, or stopped bootstrap.ps1 before its own report:
rem a PC's policy can allow no scripts, or only signed ones, or run PowerShell
rem in Constrained Language mode. install.log gets those settings.
if "%RC%"=="9009" set "FAILMSG=Windows PowerShell (powershell.exe) was not found on this PC; the install needs it" & exit /b 0
set "FAILMSG=install\bootstrap.ps1 stopped before it could report why (exit code %RC%); Windows PowerShell's message is above this box. If it says running scripts is disabled or the file is not digitally signed, a policy of this PC blocks PowerShell scripts: ask whoever manages this PC"
if not defined LOGDIR exit /b 0
>>"%LOGDIR%\install.log" echo   install\bootstrap.ps1 stopped without its report (exit code %RC%). Windows PowerShell's settings:
"%PS%" -NoProfile -ExecutionPolicy Bypass -Command "Get-ExecutionPolicy -List | ForEach-Object { '    policy ' + $_.Scope + ': ' + $_.ExecutionPolicy }; '    language mode: ' + $ExecutionContext.SessionState.LanguageMode" >>"%LOGDIR%\install.log" 2>&1
exit /b 0

:fail
call :report "THE INSTALL STOPPED"
if defined LOGDIR >>"%LOGDIR%\install.log" echo ==== finished %DATE% %TIME%: stopped ====
if not defined AUTO pause
set "QRC=1"
goto :quit

:quit
rem QRC = the exit code. The window gets back the code page it had (:log_begin)
if defined OLDCP chcp %OLDCP% <nul >nul 2>nul
exit /b %QRC%

:report
rem %1 = the title. bootstrap.ps1, install_support.py and install_components.py
rem print and save their own report (LAST-FAILURE.txt); anything else is
rem reported here, from FAILMSG or the exit code. Delayed expansion prints a
rem path with & or ) in it as it is.
if defined LOGDIR if exist "%LOGDIR%\LAST-FAILURE.txt" exit /b 0
if not defined FAILMSG set "FAILMSG=%STEP% stopped with exit code %RC%"
set "TITLE=%~1"
setlocal EnableDelayedExpansion
echo.
echo  ==========================================================================
echo   !TITLE!
echo.
echo   Step: !STEP!
echo   What happened: !FAILMSG!
echo   What to do: fix that, then !RERUN!.
if defined LOGDIR echo.
if defined LOGDIR echo   Saved to:  !LOGDIR!\LAST-FAILURE.txt
if defined LOGDIR echo   Full log:  !LOGDIR!\install.log
echo  ==========================================================================
if not defined LOGDIR goto :report_end
>>"!LOGDIR!\install.log" echo ==== FAILED %DATE% %TIME% ====
>>"!LOGDIR!\install.log" echo  Step: !STEP!
>>"!LOGDIR!\install.log" echo  What happened: !FAILMSG!
>>"!LOGDIR!\install.log" echo ==== end of failure ====
> "!LOGDIR!\LAST-FAILURE.txt" echo Kinoloom install, %DATE% %TIME%
>>"!LOGDIR!\LAST-FAILURE.txt" echo  !TITLE!
>>"!LOGDIR!\LAST-FAILURE.txt" echo.
>>"!LOGDIR!\LAST-FAILURE.txt" echo  Step: !STEP!
>>"!LOGDIR!\LAST-FAILURE.txt" echo  What happened: !FAILMSG!
>>"!LOGDIR!\LAST-FAILURE.txt" echo  What to do: fix that, then !RERUN!.
>>"!LOGDIR!\LAST-FAILURE.txt" echo.
>>"!LOGDIR!\LAST-FAILURE.txt" echo  Full log:  !LOGDIR!\install.log
:report_end
endlocal
exit /b 0

rem ------------------------------------------------------------------
:log_begin
rem logs\install\install.log keeps every run: its steps, and what went wrong.
rem A run starts without LAST-FAILURE.txt, so one that is there belongs to
rem the last run. The helpers word their advice with KINOLOOM_RERUN.
rem Python and PowerShell write both files in UTF-8, so this window's code page
rem is UTF-8 from here on too: a folder name, ver and the date then reach the
rem log as they are, not in the PC's OEM code page (which has no Chinese, say,
rem on a Western PC). :quit puts the old code page back. No prompt of this
rem script comes after this point. chcp reads the input it is given, so it gets
rem none: the answers for bootstrap.ps1's questions stay.
if not defined OLDCP for /f "tokens=2 delims=:" %%a in ('chcp ^<nul') do set "OLDCP=%%a"
if defined OLDCP set "OLDCP=%OLDCP: =%"
if defined OLDCP set "OLDCP=%OLDCP:.=%"
if defined OLDCP for /f "delims=0123456789" %%x in ("%OLDCP%") do set "OLDCP="
if defined OLDCP chcp 65001 <nul >nul 2>nul
set "LOGDIR=%DST%\logs\install"
if not exist "%LOGDIR%\" mkdir "%LOGDIR%" >nul 2>nul
if not exist "%LOGDIR%\" set "LOGDIR="
if not defined LOGDIR exit /b 0
if exist "%LOGDIR%\LAST-FAILURE.txt" del "%LOGDIR%\LAST-FAILURE.txt" >nul 2>nul
set "KINOLOOM_RERUN=%RERUN%"
>>"%LOGDIR%\install.log" echo.
>>"%LOGDIR%\install.log" echo ==== Kinoloom install %DATE% %TIME%, mode %MODE% ====
>>"%LOGDIR%\install.log" ver
>>"%LOGDIR%\install.log" echo processor: %PROCESSOR_IDENTIFIER%, %NUMBER_OF_PROCESSORS% threads (%PROCESSOR_ARCHITECTURE%)
exit /b 0

rem ------------------------------------------------------------------
:step
rem %1 = the step, %2 = what it does. The window title repeats it, so the
rem step stays in view while pip's output scrolls past.
set "STEP=step %~1 of 5, %~2"
title Kinoloom install - step %~1 of 5: %~2 - keep this window open
echo.
echo [%~1/5] %time:~0,5%  %~2...
if defined LOGDIR >>"%LOGDIR%\install.log" echo [%~1/5] %time:~0,8%  %~2
exit /b 0
