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
echo REAL MONEY MODE - PERSISTENT BINANCE LEAD / MEXC LAG
echo Trades only historically persistent lag pairs from latest lifetime CSV.
echo Strong live-event gate + exact 0/0 fee gate + convergence exit.
echo Orders use the existing REAL MEXC web-session execution path.
echo The Python runner also requires MEXC_LIVE_WRITE=YES from .env/env.
echo ==============================================================
echo.
set /p CONFIRM=Type LIVE to continue: 
if /I not "%CONFIRM%"=="LIVE" (
  echo Cancelled.
  pause
  exit /b 3
)

python -m mexc_tick_scalper.live_production_persistent --confirm-live LIVE

echo.
pause
