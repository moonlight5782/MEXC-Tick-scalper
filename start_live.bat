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

if /I not "%MEXC_LIVE_WRITE%"=="YES" (
  echo.
  echo LIVE trading is LOCKED.
  echo Set MEXC_LIVE_WRITE=YES only when you intentionally want REAL MEXC orders.
  echo.
  pause
  exit /b 2
)

echo.
echo ==============================================================
echo REAL MONEY MODE - MEXC WEB SESSION EXECUTION
echo LIVE Binance + LIVE MEXC signal. Orders go to REAL MEXC.
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
