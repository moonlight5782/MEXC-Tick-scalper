@echo off
setlocal
cd /d "%~dp0"
call .venv\Scripts\activate.bat

echo ============================================================
echo  BTW_USDT ONLY - REAL-DATA END-TO-END SHADOW
echo  LIVE Binance + LIVE MEXC market data
echo  NO REAL ORDERS - READ ONLY
echo  frozen signal alpha + CURRENT measured LIVE MEXC RTT
echo  arrival gate: ABSOLUTE EXECUTABLE EDGE, not retention percent
echo  retention/impulse retention: DIAGNOSTIC ONLY
echo  LIVE spread/depth/slippage/cost at modeled arrival
echo  single market-data process; HIGH Windows priority
echo ============================================================

for /f "usebackq delims=" %%F in (`powershell -NoProfile -Command "$f = Get-ChildItem -File 'prelive_lag_lifetime_*.csv' | Where-Object { $_.Name -ne 'prelive_lag_lifetime_BTW_ONLY.csv' } | Sort-Object LastWriteTime -Descending | Select-Object -First 1; if (-not $f) { exit 2 }; Write-Output $f.FullName"`) do set "LIFETIME_SRC=%%F"

if not defined LIFETIME_SRC (
  echo ERROR: no source prelive_lag_lifetime_*.csv found.
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
echo Latency: latest/current read-only LIVE MEXC private-path RTT, refreshed every 100ms
echo Execution: virtual IOC against LIVE MEXC depth at modeled arrival
echo Arrival rejection: reversal OR insufficient absolute edge OR depth/slippage/cost failure
echo Retention ratios: logged only; they do NOT reject an otherwise economic entry
echo Real order writes: DISABLED
echo Process priority: HIGH
echo.

start "BTW REAL-DATA SHADOW" /high /wait .venv\Scripts\python.exe -m mexc_tick_scalper.btw_economic_arrival_shadow ^
  --lifetime-csv "%BTW_LIFETIME%" ^
  --target-closed-trades 100 ^
  --session-seconds 86400 ^
  --max-signals 3000 ^
  --latency-profile latest ^
  --latency-probe-interval-ms 100 ^
  --latency-window 31 ^
  --latency-min-samples 3 ^
  --latency-max-age-seconds 1 ^
  --output persistent_end2end_BTW.csv

pause
endlocal
