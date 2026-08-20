@echo off
setlocal
cd /d "%~dp0"
call .venv\Scripts\activate.bat

echo ============================================================
echo  CURRENT AUTO STRATEGY - MEXC TESTNET EXECUTION
echo  SIGNALS: LIVE Binance + LIVE MEXC
echo  ORDERS: MEXC TESTNET ONLY - futures.testnet.mexc.com
echo  LIVE REAL-MONEY WRITES: NOT USED BY THIS RUNNER
echo.
echo  same persistent lag / 8bps / 3x signal logic
echo  same arrival economics / depth / IOC / slippage / cost gates
echo  same 100 USDT logical bank and dynamic isolated sizing
echo  same 10000 USDT historical target notional
echo  requested leverage 200x, capped by LIVE + TESTNET contract max
echo  same immediate adverse / leader retrace / residual reversal exits
echo  same profit runner: arm at +5bps; convergence disabled after arm
echo  same trailing / no-progress / timeout protection
echo  Testnet-reported liquidation price is logged per actual position
echo  no synthetic RTT sleep: actual Testnet order roundtrip is measured
echo ============================================================

for /f "usebackq delims=" %%F in (`powershell -NoProfile -Command "$f = Get-ChildItem -File 'prelive_lag_lifetime_*.csv' | Where-Object { $_.Name -notlike '*ONLY*' } | Sort-Object LastWriteTime -Descending | Select-Object -First 1; if (-not $f) { exit 2 }; Write-Output $f.FullName"`) do set "LIFETIME_SRC=%%F"

if not defined LIFETIME_SRC (
  echo ERROR: no full prelive_lag_lifetime_*.csv found.
  pause
  exit /b 2
)

echo Lifetime source: %LIFETIME_SRC%
echo.
echo REQUIRED LOCAL .env:
echo   MEXC_DEMO_WEB_TOKEN=WEB_...
echo   MEXC_DEMO_WRITE=YES
echo Never paste the Demo token into GitHub or chat.
echo.

start "AUTO MEXC TESTNET" /high /wait .venv\Scripts\python.exe -m mexc_tick_scalper.auto_discovery_testnet ^
  --lifetime-csv "%LIFETIME_SRC%" ^
  --discovery-top 5 ^
  --profit-runner-arm-bps 5 ^
  --target-closed-trades 100 ^
  --session-seconds 86400 ^
  --max-signals 3000 ^
  --latency-profile latest ^
  --latency-probe-interval-ms 100 ^
  --latency-window 31 ^
  --latency-min-samples 3 ^
  --latency-max-age-seconds 1 ^
  --testnet-position-poll-ms 250 ^
  --testnet-output persistent_end2end_TESTNET.csv

pause
endlocal
