@echo off
rem ============================================================
rem  KINOLOOM - one window, everything inside
rem  Ctrl+C or closing this window stops ALL components.
rem  Extra model roots (e.g. H3 / LTX weight folders on another
rem  drive) come from tool\settings.yaml paths.extra_model_dirs.
rem ============================================================
title Kinoloom
setlocal EnableExtensions DisableDelayedExpansion
rem INSTALL.bat is found relative to this folder, so this folder must be the
rem current one: cmd can't make a network folder (\\server\share) current,
rem and after a cd that failed, another folder's INSTALL.bat would be found.
set "HERE=%~dp0"
if "%HERE:~0,2%"=="\\" goto :network
cd /d "%~dp0" 2>nul || goto :no_cd
set "LASTFAIL=%~dp0logs\install\LAST-FAILURE.txt"

rem Run from inside the ZIP, Windows unpacks only this one file into a
rem temporary folder, and the rest of Kinoloom is not next to it.
if exist "%~dp0tool\launcher.py" goto :have_files
echo.
echo  start.bat can't find the rest of Kinoloom next to it.
echo.
echo  This happens when it runs from inside the ZIP file. Right-click the
echo  ZIP and choose "Extract All...". Then move the extracted folder (the
echo  one with INSTALL.bat in it) somewhere short, like C:\Kinoloom, and
echo  run INSTALL.bat from there.
echo.
pause
exit /b 1
:have_files

rem Installed = tool\.venv without the marker the installer keeps in it until
rem its packages are installed and import (install\install_support.py).
call :installed_check
set "IC=%errorlevel%"
if "%IC%"=="0" goto :installed
echo.
if exist "%LASTFAIL%" goto :show_failure
if "%IC%"=="2" goto :moved
if exist "%~dp0tool\.venv\" goto :unfinished
echo  Kinoloom is not installed on this PC yet. INSTALL.bat sets up
echo  everything it needs except the model weights.
goto :offer
:moved
echo  Kinoloom's Python environment (tool\.venv) no longer starts, so
echo  Kinoloom can't start. This happens when the Kinoloom folder was moved
echo  after the install. Running INSTALL.bat again builds it again.
goto :offer
:unfinished
echo  Kinoloom's install did not finish (the last run stopped before the
echo  end), so it can't start yet. Running INSTALL.bat again finishes it;
echo  the steps that are done are skipped.
goto :offer
:show_failure
echo  Kinoloom's install did not finish, so it can't start yet. The last
echo  run stopped with this (saved in logs\install\LAST-FAILURE.txt):
echo.
set "SHOW=%LASTFAIL%"
call :show_utf8
echo.
echo  Once that is sorted, running INSTALL.bat again finishes the install;
echo  the steps that are done are skipped.
:offer
echo.
set "GO="
set /p GO="Run INSTALL.bat now? [Y/n] "
rem with delayed expansion, nothing typed (a quote, an &) can break the line
setlocal EnableDelayedExpansion
if /i "!GO!"=="n" exit /b 1
if /i "!GO!"=="no" exit /b 1
endlocal
rem by its path relative to this folder (the current one): call reads a % or ^
rem in a full path as its own, and .\ finds it where Windows is set not to
rem look in the current folder (NoDefaultCurrentDirectoryInExePath)
call ".\INSTALL.bat" /fromstart
rem 0 = installed, 2 = installed but some parts failed, anything else = stopped
set "RC=%errorlevel%"
if not "%RC%"=="0" if not "%RC%"=="2" exit /b 1
call :installed_check
if errorlevel 1 exit /b 1
title Kinoloom
:installed
if exist "%~dp0tools\python\python.exe" (
    set "PY=%~dp0tools\python\python.exe"
) else (
    set "PY=%~dp0tool\.venv\Scripts\python.exe"
)
"%PY%" "%~dp0tool\launcher.py" %*
pause
exit /b

:installed_check
rem 0 = installed, 1 = not installed or not finished, 2 = tool\.venv's python no
rem longer starts (a venv made in another folder)
if not exist "%~dp0tool\.venv\Scripts\python.exe" exit /b 1
if exist "%~dp0tool\.venv\.kinoloom-incomplete" exit /b 1
"%~dp0tool\.venv\Scripts\python.exe" -c "" >nul 2>nul
if not "%errorlevel%"=="0" exit /b 2
exit /b 0

:network
echo.
echo  start.bat can't run from a network folder (a path that starts with
echo  \\). Copy the Kinoloom folder to this PC, somewhere short like
echo  C:\Kinoloom, and run INSTALL.bat there.
echo.
pause
exit /b 1

:no_cd
echo.
echo  start.bat can't open its own folder. Move the Kinoloom folder
echo  somewhere short, like C:\Kinoloom, and run INSTALL.bat there.
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
