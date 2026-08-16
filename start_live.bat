@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Virtual environment not found: .venv
  echo Run setup first.
  pause
  exit /b 1
)

call .venv\Scripts\activate.bat

echo.
echo ==============================================================
echo REAL MONEY MODE - MEXC WEB SESSION EXECUTION
echo LIVE Binance + LIVE MEXC signal. Orders go to REAL MEXC.
echo The Python runner also requires MEXC_LIVE_WRITE=YES from .env/env.
echo ==============================================================
echo.
set /p CONFIRM=Type LIVE to continue: 
if /I not "%CONFIRM%"=="LIVE" (
  echo Cancelled.
  pause
  exit /b 3
)

python -m mexc_tick_scalper.live_production_runner --confirm-live LIVE

echo.
pause
