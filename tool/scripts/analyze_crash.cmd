@echo off
rem Kinoloom - crash dump analyzer (run as Administrator)
set "ROOT=%~dp0..\.."
set "DUMPS=%ROOT%\temp\dumps"
echo Copying crash dumps...
mkdir "%DUMPS%" 2>nul
copy /Y "%SystemRoot%\Minidump\*.dmp" "%DUMPS%\" >nul 2>&1
if exist "%SystemRoot%\MEMORY.DMP" copy /Y "%SystemRoot%\MEMORY.DMP" "%DUMPS%\" >nul 2>&1
echo.
echo Looking for WinDbg / cdb...
set DBG=
where cdb 2>nul >nul && set DBG=cdb
if not defined DBG if exist "%ProgramFiles(x86)%\Windows Kits\10\Debuggers\x64\cdb.exe" set DBG="%ProgramFiles(x86)%\Windows Kits\10\Debuggers\x64\cdb.exe"
if not defined DBG if exist "%ProgramFiles%\Windows Kits\10\Debuggers\x64\cdb.exe" set DBG="%ProgramFiles%\Windows Kits\10\Debuggers\x64\cdb.exe"
if not defined DBG (
  echo WinDbg/cdb not installed. Install from the Microsoft Store:
  echo   start ms-windows-store://pdp/?ProductId=9PGJGD53TN86
  echo then re-run this script.
  pause
  exit /b
)
echo Using %DBG%
rem Analyze the newest minidump
for /f "delims=" %%f in ('dir /b /o-d "%DUMPS%\*.dmp" 2^>nul') do (
  echo Analyzing %%f ...
  echo !analyze -v > "%DUMPS%\cmds.txt"
  echo q >> "%DUMPS%\cmds.txt"
  %DBG% -z "%DUMPS%\%%f" -logo "%DUMPS%\analyze_%%f.txt" -c "!analyze -v; q" >nul 2>&1
  echo Done: %DUMPS%\analyze_%%f.txt
)
echo.
echo Analysis files written to %DUMPS%\
pause
