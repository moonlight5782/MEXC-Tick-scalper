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
echo BASELINE V1 INDEPENDENT 100-TRADE LIVE PAPER VALIDATION
echo LIVE Binance + LIVE MEXC books. NO REAL ORDERS / NO REAL FUNDS.
echo Strategy parameters are frozen in baseline_v1.py.
echo Extreme residual sanity monitor runs in a separate window.
echo ==============================================================
echo.

start "Residual sanity - baseline v1" cmd /k ".venv\Scripts\python.exe -m mexc_tick_scalper.prelive_residual_sanity --session-seconds 86400 --anomaly-residual-bps 100"

.venv\Scripts\python.exe -m mexc_tick_scalper.prelive_100_trade_shadow ^
  --target-closed-trades 100 ^
  --session-seconds 86400 ^
  --max-signals 2000

echo.
echo Main 100-trade validation finished.
echo Close the Residual sanity window after saving its CSV.
pause
