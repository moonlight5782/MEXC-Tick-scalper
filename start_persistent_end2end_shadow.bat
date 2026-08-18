@echo off
setlocal
cd /d "%~dp0"
call .venv\Scripts\activate.bat

echo ============================================================
echo  PERSISTENT BINANCE -^> LIVE MEXC END-TO-END SHADOW
echo  NO REAL ORDERS
echo  frozen alpha + entry latency + exit latency
echo ============================================================

.venv\Scripts\python.exe -m mexc_tick_scalper.persistent_end2end_shadow ^
  --target-closed-trades 100 ^
  --session-seconds 86400 ^
  --max-signals 3000 ^
  --entry-latency-ms 650 ^
  --exit-latency-ms 350 ^
  --output persistent_end2end_latency.csv

pause
endlocal
