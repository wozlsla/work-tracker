@echo off
setlocal
cd /d "%~dp0"

set "CODEX_PYTHON=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"

where py >nul 2>nul
if not errorlevel 1 (
  py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
  if not errorlevel 1 goto run_py
)

where python >nul 2>nul
if not errorlevel 1 (
  python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
  if not errorlevel 1 goto run_python
)

if exist "%CODEX_PYTHON%" goto run_codex_python

echo [WorkTracker] Python 3.11 or newer was not found.
echo Install Python, then run this file again.
exit /b 1

:run_py
echo [WorkTracker] Starting at http://127.0.0.1:8765/
py -3 -m work_tracker serve %*
exit /b %errorlevel%

:run_python
echo [WorkTracker] Starting at http://127.0.0.1:8765/
python -m work_tracker serve %*
exit /b %errorlevel%

:run_codex_python
echo [WorkTracker] Starting at http://127.0.0.1:8765/
"%CODEX_PYTHON%" -m work_tracker serve %*
exit /b %errorlevel%
