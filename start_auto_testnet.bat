@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "LOG_FILE=testnet_last_run.log"
if exist "%LOG_FILE%" del /q "%LOG_FILE%"

echo ============================================================
echo  BINANCE + MEXC PAIR SCAN -> TESTNET TRADING
echo  LIVE REAL-MONEY WRITES: NOT USED BY THIS RUNNER
echo.
echo  PRE-TRADE SCAN:
echo  finds strategy-compatible pairs on LIVE Binance + LIVE MEXC
echo  keeps only symbols also available on MEXC Testnet
echo  ranks by Signals / Med lag / Survive@RTT / Residual / Strength / Score
echo  choose pair number or symbol; plain Enter selects #1
echo.
echo  TRADING ENTRY:
echo  original baseline residual ^>= 8bps, strength ^>= 3x
echo  signal gate / arrival economics / sizing / IOC remain unchanged
echo.
echo  TRADING LATENCY RULE:
echo  scan-time sampling may wait BEFORE trading starts
echo  after selection: no synthetic RTT and no fixed software sleep
echo  confirmed fill -> position management starts immediately
echo  no blocking get_positions wait after confirmed fill
echo  HTTP/network/MEXC response time determines execution latency
echo.
echo  PROFIT HOLD:
echo  first positive executable Demo PnL -> PROFIT HOLD
echo  positive trailing stop ratchets upward
echo  convergence/retrace/reversal/no-progress/timeout do not cut winner
echo  hard mid-adverse safety remains active
echo  exchange/forced cleanup protections remain
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

.venv\Scripts\python.exe -m mexc_tick_scalper.auto_discovery_testnet_selector ^
  --discovery-top 10 ^
  --profit-runner-arm-bps 5 ^
  --target-closed-trades 100 ^
  --session-seconds 86400 ^
  --max-signals 3000 ^
  --testnet-output persistent_end2end_TESTNET_SELECTED_PROFIT_HOLD.csv 2>"%LOG_FILE%"

set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" goto :failed

echo.
echo Selected-pair Testnet runner finished normally. Exit code: %RC%
echo Error log (if any): %CD%\%LOG_FILE%
pause
exit /b 0

:failed
set "RC=%ERRORLEVEL%"
if "%RC%"=="0" set "RC=2"
echo.
echo ============================================================
echo PAIR SELECTOR / TESTNET RUNNER FAILED. Exit code: %RC%
echo Error log: %CD%\%LOG_FILE%
echo ============================================================
if exist "%LOG_FILE%" (
  echo.
  type "%LOG_FILE%"
)
echo.
pause
exit /b %RC%
