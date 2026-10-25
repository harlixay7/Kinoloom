@echo off
rem ============================================================
rem  KINOLOOM - one-click install of everything but the model
rem  weights. Safe to run again: finished steps are skipped.
rem  start.bat runs this for you when Kinoloom is not installed.
rem ============================================================
setlocal EnableExtensions DisableDelayedExpansion
title Kinoloom - install
cd /d "%~dp0"
set "FROM_START="
if /i "%~1"=="/fromstart" set "FROM_START=1"

echo ==============================================================
echo  KINOLOOM - INSTALL
echo ==============================================================
echo.
echo  This sets up everything Kinoloom needs on this PC except the
echo  model weights:
echo.
echo    - Python 3.12 for Kinoloom alone (tools\python). A Python you
echo      installed yourself is not used or changed.
echo    - Git (tools\git), if this PC has none.
echo    - the web app and its packages (tool\.venv)
echo    - the three ComfyUI render engines with their add-ons and
echo      packages, and ffmpeg
echo.
echo  It downloads about 9 GB, needs about 17 GB of disk, and takes
echo  roughly 15 to 45 minutes, depending on your internet connection
echo  and your PC. It works through 5 steps, and each one prints its
echo  number and the time it started; the window title shows the step
echo  too. pip's progress bars can stand still for a few minutes while
echo  a big package unpacks: that is normal. Keep this window open
echo  until it says it is done.
echo.
echo  It may ask you twice to allow an administrator step: to install
echo  the Microsoft Visual C++ runtime, if this PC lacks it, and to turn
echo  on Windows long paths, if this folder's path is too long. If the
echo  install stops, run INSTALL.bat again: finished steps are skipped.
echo.
echo  Press any key to start, or close this window to cancel.
pause >nul
set "T0=%time:~0,5%"

call "%~dp0install\INSTALL-MAIN.bat" /auto
if errorlevel 2 goto :partial
if errorlevel 1 goto :failed

title Kinoloom - installed
echo.
echo ==============================================================
echo  Kinoloom is installed: started %T0%, finished %time:~0,5%.
echo.
echo  Start it with start.bat: the web app opens in your browser.
echo.
echo  To make videos and images it needs the model weights. Get them
echo  with install\INSTALL-MAIN.bat, mode [3]: it lists every model
echo  with its download size and licence, and installs the ones you
echo  pick. Until then the web app lists the models as not installed.
echo ==============================================================
if defined FROM_START exit /b 0
echo.
set "GO="
set /p GO="Start Kinoloom now? [Y/n] "
if /i "%GO%"=="n" exit /b 0
if /i "%GO%"=="no" exit /b 0
start "Kinoloom" "%~dp0start.bat"
exit /b 0

:partial
title Kinoloom - install finished, some parts missing
echo.
echo ==============================================================
echo  Some parts did not install: see the messages above. Run
echo  INSTALL.bat again to retry them; finished steps are skipped.
echo  Kinoloom starts with whatever is installed.
echo ==============================================================
pause
exit /b 2

:failed
title Kinoloom - install stopped
echo.
echo ==============================================================
echo  The install stopped: see the messages above. Fix the cause and
echo  run INSTALL.bat again; finished steps are skipped.
echo ==============================================================
pause
exit /b 1
