@echo off
setlocal
cd /d "%~dp0"
call .venv\Scripts\activate.bat

REM Same-symbol Demo test:
REM auto-discover exact symbols present on Binance USD-M + LIVE MEXC + MEXC Testnet.
REM The strongest qualifying LIVE Binance->MEXC signal is traded on that SAME symbol on Testnet.
REM No proxy substitution. No Testnet activity ranking.
REM BASELINE_V1 thresholds remain unchanged.
REM BOTH Demo entry and exit fees are deducted.
REM No LIVE orders are sent.

.venv\Scripts\python.exe -m mexc_tick_scalper.demo_baseline_v1_multipair ^
  --target-closed-trades 100 ^
  --session-seconds 86400 ^
  --max-signals 3000 ^
  --demo-leverage 0 ^
  --demo-max-notional-usdt 0 ^
  --demo-ioc-cross-bps 1.0

pause
