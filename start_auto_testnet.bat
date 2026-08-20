@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "LOG_FILE=testnet_last_run.log"
if exist "%LOG_FILE%" del /q "%LOG_FILE%"

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

if not exist ".venv\Scripts\python.exe" (
  echo ERROR: .venv\Scripts\python.exe not found.
  echo ERROR: .venv\Scripts\python.exe not found.>>"%LOG_FILE%"
  goto :failed
)

set "LIFETIME_SRC="
for /f "delims=" %%F in ('dir /b /a-d /o-d "prelive_lag_lifetime_*.csv" 2^>nul ^| findstr /v /i "ONLY"') do (
  if not defined LIFETIME_SRC set "LIFETIME_SRC=%%~fF"
)

if not defined LIFETIME_SRC (
  echo ERROR: no full prelive_lag_lifetime_*.csv found in %CD%.
  echo ERROR: no full prelive_lag_lifetime_*.csv found in %CD%.>>"%LOG_FILE%"
  goto :failed
)

if not exist "%LIFETIME_SRC%" (
  echo ERROR: selected lifetime CSV does not exist: "%LIFETIME_SRC%"
  echo ERROR: selected lifetime CSV does not exist: "%LIFETIME_SRC%">>"%LOG_FILE%"
  goto :failed
)

echo Lifetime source: %LIFETIME_SRC%
echo.
echo REQUIRED LOCAL .env:
echo   MEXC_DEMO_WEB_TOKEN=WEB_...
echo   MEXC_DEMO_WRITE=YES
echo Never paste the Demo token into GitHub or chat.
echo.
echo Full stderr will also be saved to %LOG_FILE%
echo.

.venv\Scripts\python.exe -m mexc_tick_scalper.auto_discovery_testnet ^
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
  --testnet-output persistent_end2end_TESTNET.csv 2>"%LOG_FILE%"

set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" goto :failed

echo.
echo Testnet runner finished normally. Exit code: %RC%
echo Error log (if any): %CD%\%LOG_FILE%
pause
exit /b 0

:failed
set "RC=%ERRORLEVEL%"
if "%RC%"=="0" set "RC=2"
echo.
echo ============================================================
echo TESTNET RUNNER FAILED. Exit code: %RC%
echo Error log: %CD%\%LOG_FILE%
echo ============================================================
if exist "%LOG_FILE%" (
  echo.
  type "%LOG_FILE%"
)
echo.
pause
exit /b %RC%
