@echo off
setlocal
cd /d "%~dp0"
py -3 -c "import sys, tkinter; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if not errorlevel 1 goto use_launcher
python -c "import sys, tkinter; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if not errorlevel 1 goto use_python
echo Receipt Recorder needs Python 3.10 or later, including Tkinter.
echo Install Python from https://www.python.org/downloads/windows/
echo Keep the Tcl/Tk option enabled and select Add Python to PATH.
echo Then double-click start_windows.bat again.
pause
exit /b 1
:use_launcher
py -3 app.py
goto finish
:use_python
python app.py
:finish
set "APP_EXIT=%ERRORLEVEL%"
if not "%APP_EXIT%"=="0" pause
exit /b %APP_EXIT%
