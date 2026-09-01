@echo off
REM ============================================================================
REM  XAUUSD Trading System - update to the latest code.  Double-click this file.
REM
REM  Downloads the current version and copies it over this installation.
REM
REM  YOUR SETTINGS AND DATA ARE NOT TOUCHED. The copy explicitly excludes:
REM      .env      your broker credentials and generated tokens
REM      data\     the decision journal and the database
REM      logs\     engine and bridge logs
REM      .venv\    the installed Python environment
REM
REM  Nothing is ever deleted. Files are only added or overwritten, so the worst
REM  case is that you re-run Setup.bat afterwards.
REM ============================================================================
setlocal EnableDelayedExpansion
cd /d "%~dp0.."
title XAUUSD - Update

set REPO=https://github.com/LouisKruge/XAAUSD
set BRANCH=claude/xauusd-trading-bot-mbvqdd
set WORK=%TEMP%\xauusd-update
set ZIP=%WORK%\repo.zip

echo.
echo   XAUUSD Trading System - Update
echo   =============================
echo.
echo   Installation: %CD%
echo.

REM ---------------------------------------------------------------- Safety ---
if not exist "pyproject.toml" (
  echo   [X] This does not look like the installation folder.
  echo       Run Update.bat from the "windows" folder inside your install.
  echo.
  pause
  exit /b 1
)

if exist ".env" (
  echo   [OK] Found .env - your settings will be preserved
) else (
  echo   [!] No .env here yet. Run Setup.bat after this finishes.
)

REM -------------------------------------------------------------- Download ---
echo   [..] Downloading the latest version
if exist "%WORK%" rmdir /s /q "%WORK%"
mkdir "%WORK%"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ProgressPreference='SilentlyContinue';" ^
  "try { Invoke-WebRequest -Uri '%REPO%/archive/refs/heads/%BRANCH%.zip' -OutFile '%ZIP%' -UseBasicParsing }" ^
  "catch { Write-Host $_.Exception.Message; exit 1 }"
if errorlevel 1 (
  echo   [X] Download failed. Check your internet connection.
  pause
  exit /b 1
)
echo   [OK] Downloaded

REM --------------------------------------------------------------- Extract ---
echo   [..] Extracting
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ProgressPreference='SilentlyContinue';" ^
  "Expand-Archive -Path '%ZIP%' -DestinationPath '%WORK%\x' -Force"
if errorlevel 1 ( echo   [X] Could not extract the download. & pause & exit /b 1 )

REM The archive contains a single top-level folder whose name includes the
REM branch; find it rather than hard-coding it, so a rename does not break this.
set SRC=
for /d %%d in ("%WORK%\x\*") do set SRC=%%d
if "!SRC!"=="" ( echo   [X] The download looks empty. & pause & exit /b 1 )
echo   [OK] Extracted

REM ------------------------------------------------------------------ Copy ---
REM /XF and /XD are what protect your settings and data. robocopy exit codes
REM below 8 all mean success; only 8 and above are real failures.
echo   [..] Updating the program files
robocopy "!SRC!" "%CD%" /E /XD ".venv" "data" "logs" ".git" ".pytest_cache" ".mypy_cache" /XF ".env" /NFL /NDL /NJH /NJS /NP >nul
if !ERRORLEVEL! GEQ 8 (
  echo   [X] Copy failed with robocopy code !ERRORLEVEL!.
  pause
  exit /b 1
)
echo   [OK] Program files updated

REM -------------------------------------------------------------- Packages ---
REM Dependencies change between versions; skipping this is how you get an
REM import error on the next start.
if exist ".venv\Scripts\python.exe" (
  echo   [..] Updating packages - this can take a few minutes
  call .venv\Scripts\python.exe -m pip install -e ".[dev,ml,api,db,mt5]" --quiet --upgrade
  if errorlevel 1 (
    echo   [!] Package update reported a problem. Run Setup.bat if the bot will not start.
  ) else (
    echo   [OK] Packages updated
  )

  echo   [..] Checking the database
  call .venv\Scripts\python.exe -m xauusd.config.bootstrap --check-database

  echo   [..] Applying any database changes
  call .venv\Scripts\python.exe -m alembic upgrade head
  if errorlevel 1 ( echo   [!] Database update reported a problem. )
) else (
  echo   [!] No Python environment yet - run Setup.bat next.
)

rmdir /s /q "%WORK%" 2>nul

echo.
echo   ==========================================================
echo    Update complete. Your .env and data were not touched.
echo.
echo    Next: "Stop XAUUSD Bot", then "Start XAUUSD Bot",
echo    then run the pre-flight check on the System tab.
echo   ==========================================================
echo.
pause
