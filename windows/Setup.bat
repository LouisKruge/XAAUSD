@echo off
REM ============================================================================
REM  XAUUSD Trading System - one-time setup.  Double-click this file.
REM
REM  It installs the Python environment, starts the datastores, builds the
REM  database schema, generates a dashboard access token, and puts three
REM  shortcuts on your Desktop. You should never need to open a terminal
REM  again after this runs.
REM
REM  Safe to run more than once: every step checks before it acts.
REM ============================================================================
setlocal EnableDelayedExpansion
cd /d "%~dp0.."
title XAUUSD - Setup

echo.
echo   XAUUSD Trading System - Setup
echo   =============================
echo.

REM ---------------------------------------------------------------- Python ---
python --version >nul 2>&1
if errorlevel 1 (
  echo   [X] Python is not installed, or is not on your PATH.
  echo.
  echo       Install Python 3.11 from https://www.python.org/downloads/
  echo       IMPORTANT: tick "Add python.exe to PATH" on the first screen.
  echo.
  pause
  exit /b 1
)
for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo   [OK] Python !PYVER!

REM ------------------------------------------------------------ Environment ---
if not exist ".venv\Scripts\python.exe" (
  echo   [..] Creating the Python environment. This takes a few minutes.
  python -m venv .venv
  if errorlevel 1 ( echo   [X] Could not create the environment. & pause & exit /b 1 )
)
echo   [OK] Python environment

echo   [..] Installing packages. This is the slow part - please wait.
call .venv\Scripts\python.exe -m pip install --upgrade pip --quiet
call .venv\Scripts\python.exe -m pip install -e ".[dev,ml,api,db,mt5]" --quiet
if errorlevel 1 ( echo   [X] Package installation failed. & pause & exit /b 1 )
echo   [OK] Packages installed

REM ------------------------------------------------------------- Datastores ---
REM Decided BEFORE .env is written, because whether PostgreSQL is actually
REM running determines which database URL belongs in it. Writing a Postgres URL
REM on a machine with no Postgres is worse than writing none: the config files
REM already fall back to a local SQLite file, and an unreachable URL overrides
REM that and fails at the first connection.
set USE_PG=
docker --version >nul 2>&1
if errorlevel 1 (
  echo   [!] Docker Desktop was not found - using a local database file.
  echo       That is fine for everything up to demo trading. Install Docker
  echo       Desktop and run this setup again when you need PostgreSQL.
) else (
  echo   [..] Starting PostgreSQL and Redis
  call .venv\Scripts\python.exe -m xauusd.config.bootstrap >nul 2>&1
  docker compose up -d
  if errorlevel 1 (
    echo   [!] Docker could not start the datastores. Is Docker Desktop running?
    echo       Falling back to a local database file.
  ) else (
    echo   [OK] PostgreSQL and Redis running
    set USE_PG=--postgres
  )
)

REM -------------------------------------------------------------- .env file ---
REM Creates .env if missing and generates any secret that has no value yet.
REM Existing values are never overwritten.
echo   [..] Checking .env
call .venv\Scripts\python.exe -m xauusd.config.bootstrap !USE_PG!
if errorlevel 1 ( echo   [X] Could not prepare .env & pause & exit /b 1 )

REM ----------------------------------------------------------------- Schema ---
echo   [..] Building the database schema
call .venv\Scripts\python.exe -m alembic upgrade head
if errorlevel 1 (
  echo   [X] Could not build the schema. See the message above.
  pause
  exit /b 1
)
echo   [OK] Database schema

REM -------------------------------------------------------------- Shortcuts ---
echo   [..] Creating Desktop shortcuts
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0make-shortcuts.ps1" -Root "%CD%"
echo   [OK] Desktop shortcuts

echo.
echo   ==========================================================
echo    Setup complete.
echo.
echo    On your Desktop you now have:
echo      "Start XAUUSD Bot"   - starts everything, opens the dashboard
echo      "Stop XAUUSD Bot"    - stops everything
echo      "Update XAUUSD Bot"  - get the latest version, keeping your settings
echo      "Arm Live Trading"   - only when you are ready for real money
echo.
echo    Start the bot, then use the dashboard for everything else:
echo    pre-flight checks, backtests, and the validation gate are all
echo    buttons on its System tab.
echo   ==========================================================
echo.
pause
