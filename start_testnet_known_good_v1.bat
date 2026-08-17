@echo off
setlocal
cd /d "%~dp0"
call .venv\Scripts\activate.bat

REM Known-good commit 372c3b2 strategy adapted to REAL MEXC Testnet execution.
REM LIVE Binance + LIVE MEXC are signal sources; SAME symbol is opened on Testnet.
REM No LIVE writes. No paper child. No stdout mirror. No proxy substitution.
REM Actual Testnet fill/position/entry+exit fees are the execution source of truth.

.venv\Scripts\python.exe -m mexc_tick_scalper.testnet_known_good_v1 ^
  --target-closed-trades 100 ^
  --session-seconds 86400 ^
  --max-signals 3000 ^
  --demo-leverage 0

pause
