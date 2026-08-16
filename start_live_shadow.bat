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
echo LIVE MARKET / PARTIAL-IOC PAPER MODE - NO REAL ORDERS
echo Uses LIVE Binance + LIVE MEXC market data.
echo IOC LIMIT entry is simulated with partial fills; remainder cancels.
echo Position management uses ONLY the actually filled quantity.
echo Entry requires strong persistent residual AND executable edge after cost.
echo Adverse stop uses MEXC mid movement, not immediate spread crossing.
echo NO MEXC order endpoint is called. NO REAL FUNDS ARE USED.
echo ==============================================================
echo.

python -m mexc_tick_scalper.prelive_persistent_ioc_shadow --session-seconds 1800 --max-signals 300 --target-notional-usdt 10000

echo.
pause
