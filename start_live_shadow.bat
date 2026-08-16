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
echo LIVE MARKET / PERSISTENT-DEPTH PARTIAL-IOC PAPER MODE
echo LIVE Binance + LIVE MEXC books. NO REAL ORDERS / NO REAL FUNDS.
echo IOC fill is counted only when liquidity survives 2 MEXC depth updates.
echo Unfilled IOC remainder cancels; only actual simulated fill is managed.
echo Exit decision is irreversible and can flatten in partial chunks.
echo Profit factor is calculated in USDT, weighted by actual fill size.
echo ==============================================================
echo.

python -m mexc_tick_scalper.prelive_persistent_ioc_shadow_v2 --session-seconds 1800 --max-signals 300 --target-notional-usdt 10000

echo.
pause
