@echo off
setlocal
cd /d "%~dp0"
call .venv\Scripts\activate.bat

REM IMPORTANT: this does NOT choose a Demo strategy.
REM Frozen BASELINE_V1 runs on LIVE Binance + LIVE MEXC data and the Testnet only mirrors its ENTRY/EXIT decisions.
REM Demo entry+exit fees are deducted from the reported Demo PnL.
REM No LIVE orders are sent.

.venv\Scripts\python.exe -m mexc_tick_scalper.demo_baseline_v1_mirror ^
  --target-closed-trades 100 ^
  --session-seconds 86400 ^
  --max-signals 3000 ^
  --demo-leverage 0 ^
  --demo-max-notional-usdt 0 ^
  --demo-ioc-cross-bps 1.0

pause
