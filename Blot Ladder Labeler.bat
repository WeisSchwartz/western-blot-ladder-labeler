@echo off
REM Double-click this file to start the Western Blot Ladder Labeler (Windows).
REM The first time, it installs what the app needs (internet required, a minute or two).
REM Keep the window that opens; close it to quit the app.
setlocal
cd /d "%~dp0"
title Western Blot Ladder Labeler
echo ================================================
echo         Western Blot Ladder Labeler
echo ================================================

set "VPY=.venv\Scripts\python.exe"
set "PYTHONUNBUFFERED=1"
if exist "%VPY%" (
  "%VPY%" -c "import flask, numpy, scipy, PIL, tifffile" >nul 2>nul && goto run
)

echo.
echo First-time setup: installing the app (needs an internet connection)...
echo.
set "PYCMD="
where py >nul 2>nul && set "PYCMD=py -3"
if not defined PYCMD (
  where python >nul 2>nul && set "PYCMD=python"
)
if not defined PYCMD goto nopython
%PYCMD% -c "import sys; sys.exit(sys.version_info < (3, 9))" >nul 2>nul || goto nopython

%PYCMD% -m venv --clear .venv || goto setupfail
"%VPY%" -m pip install --quiet --upgrade pip || goto setupfail
"%VPY%" -m pip install --quiet -e . || goto setupfail
echo Setup complete.

:run
echo.
echo Starting... (this takes a few seconds)
"%VPY%" launch.py || goto runfail
exit /b 0

:nopython
echo.
echo Python 3.9 or newer was not found.
echo Install Python from https://www.python.org/downloads/
echo (tick "Add python.exe to PATH" in the installer), then double-click this file again.
pause
exit /b 1

:setupfail
echo.
echo Setup did not finish. Check your internet connection and try again.
echo If it keeps failing, send a screenshot of this window to whoever shared the app.
pause
exit /b 1

:runfail
echo.
echo The app stopped with an error (see above).
pause
exit /b 1
