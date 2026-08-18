@echo off
setlocal
cd /d "%~dp0"
call .venv\Scripts\activate.bat

echo ============================================================
echo  PERSISTENT BINANCE -^> LIVE MEXC END-TO-END SHADOW
echo  NO REAL ORDERS
echo  frozen alpha + REALTIME measured entry/exit latency
echo ============================================================

.venv\Scripts\python.exe -m mexc_tick_scalper.persistent_end2end_shadow ^
  --target-closed-trades 100 ^
  --session-seconds 86400 ^
  --max-signals 3000 ^
  --latency-profile p75 ^
  --latency-probe-interval-ms 250 ^
  --latency-window 31 ^
  --latency-min-samples 5 ^
  --latency-max-age-seconds 2 ^
  --output persistent_end2end_latency.csv

pause
endlocal
