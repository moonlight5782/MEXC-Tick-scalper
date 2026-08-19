@echo off
setlocal
cd /d "%~dp0"
call .venv\Scripts\activate.bat

echo ============================================================
echo  BTW_USDT ONLY - PERSISTENT END-TO-END SHADOW
echo  LIVE Binance + LIVE MEXC market data
echo  NO REAL ORDERS
echo  frozen alpha + REALTIME measured entry/exit latency
echo ============================================================

for /f "usebackq delims=" %%F in (`powershell -NoProfile -Command "$f = Get-ChildItem -File 'prelive_lag_lifetime_*.csv' | Sort-Object LastWriteTime -Descending | Select-Object -First 1; if (-not $f) { exit 2 }; Write-Output $f.FullName"`) do set "LIFETIME_SRC=%%F"

if not defined LIFETIME_SRC (
  echo ERROR: no prelive_lag_lifetime_*.csv found.
  pause
  exit /b 2
)

set "BTW_LIFETIME=%CD%\prelive_lag_lifetime_BTW_ONLY.csv"

powershell -NoProfile -Command "$rows = Import-Csv -LiteralPath $env:LIFETIME_SRC | Where-Object { $_.symbol -eq 'BTW_USDT' }; if (-not $rows) { Write-Error 'BTW_USDT is absent from latest lifetime profile'; exit 3 }; $rows | Export-Csv -LiteralPath $env:BTW_LIFETIME -NoTypeInformation -Encoding UTF8"
if errorlevel 1 (
  echo ERROR: failed to build BTW-only lifetime profile from:
  echo %LIFETIME_SRC%
  pause
  exit /b 3
)

echo Lifetime source: %LIFETIME_SRC%
echo Filtered pair: BTW_USDT ONLY
echo Market data: LIVE Binance + LIVE MEXC
echo.

.venv\Scripts\python.exe -m mexc_tick_scalper.persistent_end2end_shadow ^
  --lifetime-csv "%BTW_LIFETIME%" ^
  --target-closed-trades 100 ^
  --session-seconds 86400 ^
  --max-signals 3000 ^
  --latency-profile p75 ^
  --latency-probe-interval-ms 250 ^
  --latency-window 31 ^
  --latency-min-samples 5 ^
  --latency-max-age-seconds 2 ^
  --output persistent_end2end_BTW.csv

pause
endlocal
