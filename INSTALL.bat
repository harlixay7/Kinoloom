@echo off
rem ============================================================
rem  KINOLOOM - one-click install of everything but the model
rem  weights. Safe to run again: finished steps are skipped.
rem  start.bat runs this for you when Kinoloom is not installed.
rem  Every run is logged in logs\install\.
rem ============================================================
setlocal EnableExtensions DisableDelayedExpansion
title Kinoloom - install
set "FROM_START="
if /i "%~1"=="/fromstart" set "FROM_START=1"

rem The next script is found relative to this folder, so this folder must be
rem the current one: cmd can't make a network folder (\\server\share) current,
rem and after a cd that failed, another folder's scripts would be found.
set "HERE=%~dp0"
if "%HERE:~0,2%"=="\\" goto :network
cd /d "%~dp0" 2>nul || goto :no_cd

rem Run from inside the ZIP, Windows unpacks only this one file into a
rem temporary folder, and the rest of Kinoloom is not next to it.
if exist "%~dp0install\INSTALL-MAIN.bat" goto :have_files
echo.
echo  INSTALL.bat can't find the rest of Kinoloom next to it, so it can't
echo  install anything.
echo.
echo  This happens when it runs from inside the ZIP file. Right-click the
echo  ZIP and choose "Extract All...". Then move the extracted folder (the
echo  one with INSTALL.bat in it) somewhere short, like C:\Kinoloom, and
echo  run INSTALL.bat from there.
echo.
pause
exit /b 1
:have_files

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
echo  It downloads about 6.3 GB and takes roughly 20 to 60 minutes,
echo  longer on a slow connection. It needs about 30 GB of free disk
echo  while it runs; 17 GB stay once it is done. A download that breaks
echo  off is tried again. It works through 5 steps, and each one prints
echo  its number and the time it started; the window title shows the
echo  step too. pip's progress bars can stand still for a few minutes
echo  while a big package unpacks: that is normal. Keep this window
echo  open until it says it is done.
echo.
echo  It may ask you twice to allow an administrator step: to install
echo  the Microsoft Visual C++ runtime, if this PC lacks it, and to turn
echo  on Windows long paths, if this folder's path is too long. If the
echo  install stops, it says why, and saves that in logs\install. Run
echo  INSTALL.bat again then: finished steps are skipped.
echo.
echo  Press any key to start, or close this window to cancel.
pause >nul
set "T0=%time:~0,5%"

rem by its path relative to this folder (the current one): call reads a % or ^
rem in a full path as its own, and .\ finds it where Windows is set not to
rem look in the current folder (NoDefaultCurrentDirectoryInExePath)
call ".\install\INSTALL-MAIN.bat" /auto
rem 0 = installed, 2 = installed but some parts failed, anything else = stopped
set "RC=%errorlevel%"
if "%RC%"=="0" goto :installed
if "%RC%"=="2" goto :partial
goto :failed

:installed
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
echo  Already have some, from ComfyUI say? Mode [3] uses those instead
echo  of downloading them, under any name and in any folder.
echo ==============================================================
if defined FROM_START exit /b 0
echo.
set "GO="
set /p GO="Start Kinoloom now? [Y/n] "
rem with delayed expansion, nothing typed (a quote, an &) can break the line
setlocal EnableDelayedExpansion
if /i "!GO!"=="n" exit /b 0
if /i "!GO!"=="no" exit /b 0
endlocal
rem in this window, by its relative path like INSTALL-MAIN.bat above
call ".\start.bat"
exit /b 0

:partial
title Kinoloom - install finished, some parts missing
rem the report scrolled up behind the install check: show it again
set "SHOW=%~dp0logs\install\LAST-FAILURE.txt"
if exist "%SHOW%" call :show_utf8
echo.
echo ==============================================================
echo  Some parts did not install: see above (saved in
echo  logs\install\LAST-FAILURE.txt). Run INSTALL.bat again to retry
echo  them; finished steps are skipped. Kinoloom starts with whatever
echo  is installed.
echo ==============================================================
pause
exit /b 2

:failed
title Kinoloom - install stopped
echo.
echo ==============================================================
echo  The install stopped. The report above says why and what to
echo  do; it is saved in logs\install\LAST-FAILURE.txt, and the whole
echo  run in logs\install\install.log. Run INSTALL.bat again once
echo  that is sorted: finished steps are skipped.
echo ==============================================================
pause
exit /b 1

:network
echo.
echo  INSTALL.bat can't run from a network folder (a path that starts
echo  with \\), so it did not install anything. Copy the Kinoloom folder
echo  to this PC, somewhere short like C:\Kinoloom, and run INSTALL.bat
echo  there.
echo.
pause
exit /b 1

:no_cd
echo.
echo  INSTALL.bat can't open its own folder, so it did not install
echo  anything. Move the Kinoloom folder somewhere short, like
echo  C:\Kinoloom, and run INSTALL.bat there.
echo.
pause
exit /b 1

:show_utf8
rem SHOW = a report in UTF-8 (what the installer writes): typed with this window
rem in UTF-8, then the window gets its code page back. chcp reads the input it is
rem given, so it gets none: an answer typed or piped for a later question stays.
set "CP0="
for /f "tokens=2 delims=:" %%a in ('chcp ^<nul') do set "CP0=%%a"
if defined CP0 set "CP0=%CP0: =%"
if defined CP0 set "CP0=%CP0:.=%"
if defined CP0 for /f "delims=0123456789" %%x in ("%CP0%") do set "CP0="
if defined CP0 chcp 65001 <nul >nul 2>nul
type "%SHOW%"
if defined CP0 chcp %CP0% <nul >nul 2>nul
exit /b 0
