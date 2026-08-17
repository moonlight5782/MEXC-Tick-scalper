@echo off
setlocal
cd /d "%~dp0"
call .venv\Scripts\activate.bat

REM Frozen BASELINE_V1 with DIRECT same-symbol MEXC Testnet execution.
REM One process only: no paper child, no stdout mirror, no proxy symbol.
REM Signals use LIVE Binance + LIVE MEXC market data.
REM Accepted ENTRY sends a REAL IOC on the SAME symbol to MEXC Testnet.
REM Frozen BASELINE_V1 RTT/retention/IOC/slippage/cost/trailing/exit thresholds stay unchanged.
REM Testnet entry + exit fees are deducted from reported PnL.
REM No LIVE orders are sent.

.venv\Scripts\python.exe -m mexc_tick_scalper.demo_baseline_v1_direct ^
  --target-closed-trades 100 ^
  --session-seconds 86400 ^
  --max-signals 3000 ^
  --demo-leverage 0

pause
