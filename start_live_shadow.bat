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
echo LIVE MARKET / PAPER MODE - NO REAL ORDERS
echo Uses LIVE Binance + LIVE MEXC market data.
echo Uses read-only MEXC private requests for fee/RTT checks.
echo NO MEXC order endpoint is called. NO REAL FUNDS ARE USED.
echo Persistent-lag pair filter + strong event gate + convergence exits.
echo ==============================================================
echo.

python -m mexc_tick_scalper.prelive_persistent_pair_shadow --session-seconds 1800 --max-signals 300 --target-notional-usdt 10000

echo.
pause
