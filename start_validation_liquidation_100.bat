@echo off
setlocal
cd /d "%~dp0"
call .venv\Scripts\activate.bat
.venv\Scripts\python.exe -m mexc_tick_scalper.prelive_liquidation_validation_v4 --target-closed-trades 100 --balance-usdt 100 --initial-margin-usdt 50 --leverage 0 --session-seconds 86400 --max-signals 3000 --max-arrival-spread-bps 20 --max-roundtrip-cost-bps 25
pause
