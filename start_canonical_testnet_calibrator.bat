@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo ERROR: .venv not found. Run: py -3.11 -m venv .venv ^&^& .venv\Scripts\python.exe -m pip install -e . pytest
  exit /b 2
)

call .venv\Scripts\activate.bat

echo ============================================================
echo  CANONICAL MEXC TESTNET EXECUTION CALIBRATOR
echo  TESTNET WRITES ONLY / NOT STRATEGY PNL VALIDATION
echo ============================================================

.venv\Scripts\python.exe -m mexc_tick_scalper.canonical_testnet_calibrator ^
  --symbol BTC_USDT ^
  --rounds 5 ^
  --notional-usdt 1000 ^
  --leverage 10 ^
  --ioc-cross-bps 1 ^
  --output canonical_testnet_execution.csv

set RC=%ERRORLEVEL%
pause
endlocal & exit /b %RC%
