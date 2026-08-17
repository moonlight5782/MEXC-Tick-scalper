@echo off
setlocal
cd /d "%~dp0"
call .venv\Scripts\activate.bat

REM Source of truth: unchanged successful multi-pair LIVE-paper BASELINE_V1 runner.
REM Testnet does NOT restrict which LIVE symbols may signal.
REM Demo is execution-only: real IOC/partial-fill/close/latency/fee accounting through MEXC Demo web token.
REM BOTH Demo entry and exit fees are deducted.
REM BTC_USDT is used only as the visible Testnet execution proxy; it does not change signal selection.
REM The run STOPS if a baseline ENTRY cannot open a real Testnet IOC; no paper-only continuation.
REM No LIVE orders are sent.

.venv\Scripts\python.exe -m mexc_tick_scalper.demo_live_baseline_execution_strict ^
  --target-closed-trades 100 ^
  --session-seconds 86400 ^
  --max-signals 3000 ^
  --demo-proxy-symbol BTC_USDT ^
  --activity-sample-seconds 4 ^
  --demo-leverage 0 ^
  --demo-max-notional-usdt 0 ^
  --demo-ioc-cross-bps 1.0

pause
