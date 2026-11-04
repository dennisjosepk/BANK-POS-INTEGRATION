@echo off
setlocal
cd /d "%~dp0"
py -3 -m venv .venv-build
if errorlevel 1 goto failed
.venv-build\Scripts\python.exe -m pip install -r requirements-build.txt
if errorlevel 1 goto failed
.venv-build\Scripts\python.exe -m unittest discover -s tests -v
if errorlevel 1 goto failed
.venv-build\Scripts\python.exe -m PyInstaller --noconfirm --clean --onefile --windowed --name ReceiptRecorder app.py
if errorlevel 1 goto failed
echo.
echo Built dist\ReceiptRecorder.exe. Copy it to your Windows computer and double-click it.
echo That computer does not need Python installed.
pause
exit /b 0
:failed
echo.
echo Build failed. Read the error above. Use Python 3.12 on Windows to build this app.
pause
exit /b 1
