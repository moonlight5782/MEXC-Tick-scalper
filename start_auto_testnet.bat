@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "LOG_FILE=testnet_last_run.log"
if exist "%LOG_FILE%" del /q "%LOG_FILE%"

echo ============================================================
echo  FIXED XRP_USDT - MEXC TESTNET EXECUTION TEST
echo  SIGNALS: LIVE Binance XRPUSDT + LIVE MEXC XRP_USDT
echo  ORDERS: MEXC TESTNET XRP_USDT ONLY
echo  LIVE REAL-MONEY WRITES: NOT USED BY THIS RUNNER
echo.
echo  TEMP ONLY: AUTO pair selection / zero-fee eligibility bypassed
echo  LIVE prices are signal/retrace/convergence only
echo  DEMO prices are slippage/PnL/trailing/execution only
echo  logical bank uses GROSS zero-fee strategy PnL
echo  Demo entry+exit fees and Demo net PnL are reported separately
echo  same persistent lag / 8bps / 3x signal logic
echo  same arrival economics / depth / IOC / cost gates
echo  same 100 USDT logical bank and dynamic isolated sizing
echo  same 10000 USDT historical target notional
echo  requested leverage 200x, capped by LIVE + TESTNET XRP max
echo  same immediate adverse / leader retrace / residual reversal exits
echo  same profit runner: arm at +5bps; convergence disabled after arm
echo  same trailing / no-progress / timeout protection
echo  no synthetic RTT sleep: actual Testnet order roundtrip is measured
echo ============================================================

if not exist ".venv\Scripts\python.exe" (
  echo ERROR: .venv\Scripts\python.exe not found.
  echo ERROR: .venv\Scripts\python.exe not found.>>"%LOG_FILE%"
  goto :failed
)

echo REQUIRED LOCAL .env:
echo   MEXC_DEMO_WEB_TOKEN=WEB_...
echo   MEXC_DEMO_WRITE=YES
echo Never paste the Demo token into GitHub or chat.
echo.
echo Full stderr will also be saved to %LOG_FILE%
echo.

.venv\Scripts\python.exe -m mexc_tick_scalper.auto_discovery_testnet_xrp_fixed ^
  --profit-runner-arm-bps 5 ^
  --target-closed-trades 100 ^
  --session-seconds 86400 ^
  --max-signals 3000 ^
  --testnet-position-poll-ms 250 ^
  --testnet-output persistent_end2end_TESTNET_XRP_FIXED.csv 2>"%LOG_FILE%"

set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" goto :failed

echo.
echo Fixed XRP Testnet runner finished normally. Exit code: %RC%
echo Error log (if any): %CD%\%LOG_FILE%
pause
exit /b 0

:failed
set "RC=%ERRORLEVEL%"
if "%RC%"=="0" set "RC=2"
echo.
echo ============================================================
echo FIXED XRP TESTNET RUNNER FAILED. Exit code: %RC%
echo Error log: %CD%\%LOG_FILE%
echo ============================================================
if exist "%LOG_FILE%" (
  echo.
  type "%LOG_FILE%"
)
echo.
pause
exit /b %RC%
