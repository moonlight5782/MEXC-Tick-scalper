@echo off
setlocal
cd /d "%~dp0"
call .venv\Scripts\activate.bat

REM Reference behavior: successful multi-pair LIVE-paper BASELINE_V1 runner.
REM Demo test scans multiple Testnet+LIVE+Binance pairs concurrently.
REM BASELINE_V1 thresholds are not loosened.
REM Demo entry+exit fees are deducted from reported Demo PnL.
REM No LIVE orders are sent.

.venv\Scripts\python.exe -m mexc_tick_scalper.demo_baseline_v1_multipair ^
  --target-closed-trades 100 ^
  --session-seconds 86400 ^
  --max-signals 3000 ^
  --demo-universe-size 20 ^
  --activity-sample-seconds 4 ^
  --demo-leverage 0 ^
  --demo-max-notional-usdt 0 ^
  --demo-ioc-cross-bps 1.0

pause
