@echo off
REM Windows wrapper → demo/run_hands_off.py
setlocal
cd /d "%~dp0\.."
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" "demo\run_hands_off.py" %*
  exit /b %ERRORLEVEL%
)
python "demo\run_hands_off.py" %*
exit /b %ERRORLEVEL%
