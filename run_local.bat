@echo off
setlocal

cd /d "%~dp0"

set "PYTHON=python"
if exist "%~dp0venv\Scripts\python.exe" set "PYTHON=%~dp0venv\Scripts\python.exe"
if exist "%~dp0.venv\Scripts\python.exe" set "PYTHON=%~dp0.venv\Scripts\python.exe"

echo Using Python: %PYTHON%
echo Starting Django development server on http://127.0.0.1:8000/

"%PYTHON%" manage.py runserver

endlocal