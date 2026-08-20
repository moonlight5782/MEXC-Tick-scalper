@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "LOG_FILE=testnet_last_run.log"
if exist "%LOG_FILE%" del /q "%LOG_FILE%"

echo ============================================================
echo  FAST XRP_USDT - MEXC TESTNET EXECUTION TEST
echo  SIGNALS: LIVE Binance XRPUSDT + LIVE MEXC XRP_USDT
echo  ORDERS: MEXC TESTNET XRP_USDT ONLY
echo  LIVE REAL-MONEY WRITES: NOT USED BY THIS RUNNER
echo.
echo  TEMP ONLY: AUTO pair selection / zero-fee eligibility bypassed
echo  RESTORED entry gate: residual >= 15bps, strength >= 4x
echo  Entry logic is unchanged from the run that was opening trades
echo  LIVE prices are signal/thesis only
echo  DEMO prices are execution/slippage/PnL/trailing only
echo  Demo best price is cached before the signal critical path
echo  extra post-fill get_positions RTT removed from entry timing
echo  WINNER POLICY: first positive executable PnL -> profit hold
echo  profitable position is held and trailing stop only ratchets upward
echo  convergence/retrace/reversal/no-progress do not cut a winner
echo  hard safety protections remain enabled
echo  logical bank uses GROSS zero-fee strategy PnL
echo  Demo entry+exit fees and Demo net PnL are reported separately
echo  same arrival economics / depth / IOC / cost gates
echo  same 100 USDT logical bank and dynamic isolated sizing
echo  same 10000 USDT historical target notional
echo  requested leverage 200x, capped by LIVE + TESTNET XRP max
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

.venv\Scripts\python.exe -m mexc_tick_scalper.auto_discovery_testnet_xrp_entry15 ^
  --profit-runner-arm-bps 5 ^
  --target-closed-trades 100 ^
  --session-seconds 86400 ^
  --max-signals 3000 ^
  --testnet-position-poll-ms 250 ^
  --testnet-output persistent_end2end_TESTNET_XRP_FAST.csv 2>"%LOG_FILE%"

set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" goto :failed

echo.
echo Fast XRP Testnet runner finished normally. Exit code: %RC%
echo Error log (if any): %CD%\%LOG_FILE%
pause
exit /b 0

:failed
set "RC=%ERRORLEVEL%"
if "%RC%"=="0" set "RC=2"
echo.
echo ============================================================
echo FAST XRP TESTNET RUNNER FAILED. Exit code: %RC%
echo Error log: %CD%\%LOG_FILE%
echo ============================================================
if exist "%LOG_FILE%" (
  echo.
  type "%LOG_FILE%"
)
echo.
pause
exit /b %RC%
