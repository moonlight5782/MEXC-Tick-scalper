@echo off
setlocal
cd /d "%~dp0"
call .venv\Scripts\activate.bat

echo ============================================================
echo  AUTO DISCOVERY - REAL-DATA END-TO-END SHADOW
echo  LIVE Binance + LIVE MEXC market data
echo  NO REAL ORDERS - READ ONLY
echo  scans current exact-0/0 Binance-crosslisted MEXC futures
echo  ranks persistent lag against CURRENT measured LIVE MEXC RTT
echo  bank: 100 USDT; isolated margin
echo  historical target notional: 10000 USDT
echo  requested leverage: 200x; effective leverage capped per LIVE contract
echo  emergency/reversal exit: DECIDE IMMEDIATELY on first observed bad market event
echo  exit execution delay: measured network/exchange latency only; no synthetic hold
echo  same symbol stays monitored after close and may trade again after a NEW re-armed impulse
echo  requested/fill reports use ACTUAL dynamic risk-sized notional
echo  floating stop: LIVE-spread trailing distance
echo ============================================================

for /f "usebackq delims=" %%F in (`powershell -NoProfile -Command "$f = Get-ChildItem -File 'prelive_lag_lifetime_*.csv' | Where-Object { $_.Name -notlike '*ONLY*' } | Sort-Object LastWriteTime -Descending | Select-Object -First 1; if (-not $f) { exit 2 }; Write-Output $f.FullName"`) do set "LIFETIME_SRC=%%F"

if not defined LIFETIME_SRC (
  echo ERROR: no full prelive_lag_lifetime_*.csv found.
  pause
  exit /b 2
)

echo Lifetime source: %LIFETIME_SRC%
echo Discovery: top 5 current-latency persistent candidates
echo Latency: latest/current read-only LIVE MEXC private-path RTT, refreshed every 100ms
echo Execution: virtual IOC against LIVE MEXC depth at modeled arrival
echo Real order writes: DISABLED
echo.

start "AUTO REAL-DATA SHADOW" /high /wait .venv\Scripts\python.exe -m mexc_tick_scalper.auto_discovery_shadow_v2 ^
  --lifetime-csv "%LIFETIME_SRC%" ^
  --discovery-top 5 ^
  --target-closed-trades 100 ^
  --session-seconds 86400 ^
  --max-signals 3000 ^
  --latency-profile latest ^
  --latency-probe-interval-ms 100 ^
  --latency-window 31 ^
  --latency-min-samples 3 ^
  --latency-max-age-seconds 1 ^
  --output persistent_end2end_AUTO.csv

pause
endlocal
