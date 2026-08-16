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
echo LIVE MARKET / PAPER CATCH-UP MODE - NO REAL ORDERS
echo Uses LIVE Binance + LIVE MEXC market data.
echo Uses read-only MEXC private requests for fee/RTT checks.
echo NO MEXC order endpoint is called. NO REAL FUNDS ARE USED.
echo Requires persistent pair + strong event + retained Binance impulse.
echo Convergence is valid only when MEXC actually catches up.
echo ==============================================================
echo.

python -m mexc_tick_scalper.prelive_persistent_catchup_shadow --session-seconds 1800 --max-signals 300 --target-notional-usdt 10000

echo.
pause
