@echo off
REM Start the TeamMeetingAI server (FastAPI + background job runner).
REM
REM Configuration comes from the repository-root .env file (DATABASE_URL,
REM TEAMMEETINGAI_SARVAM_API_KEY and the optional analysis settings).  uvicorn
REM loads it through python-dotenv without overriding real environment
REM variables, so anything already exported in the shell still wins.
REM
REM Secrets are never echoed here: .env values are read by the process only.

setlocal

cd /d "%~dp0.."

if not exist ".venv\Scripts\python.exe" goto no_venv

REM Fail fast with an actionable message instead of a traceback mid-startup.
".venv\Scripts\python.exe" -c "import alembic, dotenv, fastapi, psycopg, sqlalchemy, uvicorn" 2>nul
if errorlevel 1 goto no_deps

set "TMA_ENV_FILE=%CD%\.env"
if not exist "%TMA_ENV_FILE%" goto no_env

echo Loading server configuration from "%TMA_ENV_FILE%" - values are not printed.
set "TMA_ENV_ARGS=--env-file "%TMA_ENV_FILE%""
goto run

:no_env
echo WARNING: "%TMA_ENV_FILE%" was not found.
echo The server will only see variables already set in this shell.
echo Copy .env.example to .env and fill in DATABASE_URL and TEAMMEETINGAI_SARVAM_API_KEY.
set "TMA_ENV_ARGS="

:run
REM Migrations must already be applied, otherwise the server refuses to start:
REM   .venv\Scripts\python.exe -m alembic upgrade head
set PYTHONUNBUFFERED=1
".venv\Scripts\python.exe" -m uvicorn api.app:app --host 0.0.0.0 --port 8000 %TMA_ENV_ARGS%
exit /b %ERRORLEVEL%

:no_venv
echo ERROR: .venv not found in "%CD%".
echo Create it with: python -m venv .venv
echo Then install dependencies: .venv\Scripts\python.exe -m pip install -r requirements.txt
exit /b 1

:no_deps
echo ERROR: server dependencies are missing.
echo Install them with: .venv\Scripts\python.exe -m pip install -r requirements.txt
exit /b 1
