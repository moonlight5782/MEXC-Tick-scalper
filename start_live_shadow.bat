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
echo LIVE MARKET / EXACT 100-TRADE ARRIVAL-BOOK IOC PAPER TEST
echo LIVE Binance + LIVE MEXC books. NO REAL ORDERS / NO REAL FUNDS.
echo Entry uses CURRENT MEXC order book at simulated order-arrival time.
echo IOC limit cross is capped at 1.00 bps; avg entry slippage <= 1.00 bps.
echo Unfilled IOC remainder cancels; only actual simulated fill is managed.
echo Exit decision is irreversible and can flatten in partial chunks.
echo Profit factor is calculated in USDT, weighted by actual fill size.
echo Test stops after EXACTLY 100 closed virtual trades.
echo ==============================================================
echo.

python -m mexc_tick_scalper.prelive_100_trade_shadow ^
  --target-closed-trades 100 ^
  --session-seconds 86400 ^
  --max-signals 2000 ^
  --target-notional-usdt 10000

echo.
pause
