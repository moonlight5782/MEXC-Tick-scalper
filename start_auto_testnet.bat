@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "LOG_FILE=testnet_last_run.log"
if exist "%LOG_FILE%" del /q "%LOG_FILE%"

echo ============================================================
echo  XRP_USDT - VERIFIED 8/3 ENTRY + PROFIT HOLD
echo  SIGNALS: LIVE Binance XRPUSDT + LIVE MEXC XRP_USDT
echo  ORDERS: MEXC TESTNET XRP_USDT ONLY
echo  LIVE REAL-MONEY WRITES: NOT USED BY THIS RUNNER
echo.
echo  ENTRY: original baseline residual >= 8bps, strength >= 3x
echo  Signal gate / arrival economics / sizing / IOC are unchanged
echo  from auto_discovery_testnet_xrp_fixed that was opening trades.
echo  confirm_ms=15 and confirm_updates=2 are original signal validation,
echo  not synthetic execution latency.
echo.
echo  LATENCY:
echo  no synthetic RTT / no fixed sleep between order-status polls
echo  no fixed sleep between position-resolution polls
echo  no fixed sleep between residual-close polls
echo  HTTP/network/MEXC response time determines observed execution latency
echo.
echo  PROFIT HOLD:
echo  first positive executable Demo PnL -> PROFIT HOLD
echo  positive trailing stop ratchets upward
echo  convergence/retrace/reversal/no-progress/timeout do not cut winner
echo  hard mid-adverse safety remains active
echo  pre-profit protections remain original
echo  exchange/forced cleanup protections remain
echo.
echo  LIVE prices are signal/thesis only
echo  DEMO prices are execution/slippage/PnL/trailing only
echo  logical bank uses GROSS zero-fee strategy PnL
echo  Demo fees and Demo net are reported separately
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

.venv\Scripts\python.exe -m mexc_tick_scalper.auto_discovery_testnet_xrp_profit_hold ^
  --profit-runner-arm-bps 5 ^
  --target-closed-trades 100 ^
  --session-seconds 86400 ^
  --max-signals 3000 ^
  --testnet-position-poll-ms 250 ^
  --testnet-output persistent_end2end_TESTNET_XRP_PROFIT_HOLD.csv 2>"%LOG_FILE%"

set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" goto :failed

echo.
echo XRP Testnet verified profit-hold runner finished normally. Exit code: %RC%
echo Error log (if any): %CD%\%LOG_FILE%
pause
exit /b 0

:failed
set "RC=%ERRORLEVEL%"
if "%RC%"=="0" set "RC=2"
echo.
echo ============================================================
echo XRP VERIFIED TESTNET RUNNER FAILED. Exit code: %RC%
echo Error log: %CD%\%LOG_FILE%
echo ============================================================
if exist "%LOG_FILE%" (
  echo.
  type "%LOG_FILE%"
)
echo.
pause
exit /b %RC%
