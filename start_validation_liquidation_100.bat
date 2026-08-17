@echo off
setlocal
cd /d "%~dp0"
call .venv\Scripts\activate.bat
.venv\Scripts\python.exe -m mexc_tick_scalper.prelive_liquidation_validation_v2 --target-closed-trades 100 --balance-usdt 100 --margin-fraction 1.0 --leverage 0 --session-seconds 86400 --max-signals 3000
pause
