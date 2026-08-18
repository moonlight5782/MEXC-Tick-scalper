@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo ERROR: .venv not found. Run: py -3.11 -m venv .venv ^&^& .venv\Scripts\python.exe -m pip install -e . pytest
  exit /b 2
)

call .venv\Scripts\activate.bat

if not exist "prelive_lag_lifetime_*.csv" (
  echo ============================================================
  echo  NO CURRENT PERSISTENT-LAG PROFILE FOUND
  echo  Building one from LIVE Binance + LIVE MEXC READ-ONLY data
  echo ============================================================
  .venv\Scripts\python.exe -m mexc_tick_scalper.canonical_bootstrap ^
    --session-seconds 900 ^
    --max-signals 120
  if errorlevel 1 exit /b %errorlevel%
)

echo ============================================================
echo  CANONICAL PERSISTENT BINANCE -^> LIVE MEXC E2E SHADOW
echo  frozen BASELINE_V1 / realtime latency / NO REAL ORDERS
echo ============================================================

.venv\Scripts\python.exe -m mexc_tick_scalper.canonical_entrypoint ^
  --target-closed-trades 100 ^
  --session-seconds 86400 ^
  --max-signals 3000 ^
  --latency-probe-interval-ms 250 ^
  --latency-window 31 ^
  --latency-min-samples 5 ^
  --latency-max-age-ms 2000 ^
  --output canonical_end2end.csv

set RC=%ERRORLEVEL%
pause
endlocal & exit /b %RC%
