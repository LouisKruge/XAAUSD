@echo off
REM ============================================================================
REM  Key 2 of the two-key live arming.
REM
REM  This stays a deliberate act at the machine, with typed confirmations,
REM  rather than a button in the dashboard. Putting it in the web UI would
REM  collapse both keys into one channel, which is the thing the design exists
REM  to prevent.
REM ============================================================================
cd /d "%~dp0.."
title XAUUSD - Arm Live Trading

echo.
echo   REAL MONEY
echo   ==========
echo.
echo   This authorises the system to place orders on a live account.
echo.
echo   Before continuing, confirm all of the following:
echo     - a strategy has PASSED the validation gate (System tab, dashboard)
echo     - you have completed at least 2 weeks of paper trading
echo     - you have completed at least 30 demo trades
echo     - the account below is funded with money you can afford to lose
echo.
set /p ACCOUNT=  MT5 account number (blank to cancel): 
if "%ACCOUNT%"=="" ( echo   Cancelled. & pause & exit /b 0 )

call .venv\Scripts\python.exe -m xauusd.cli arm-live %ACCOUNT%
echo.
pause
