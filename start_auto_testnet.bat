@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "LOG_FILE=testnet_last_run.log"
if exist "%LOG_FILE%" del /q "%LOG_FILE%"

echo ============================================================
echo  STRUCTURED TESTNET APP
echo  universe -^> scan -^> pair selection -^> trading
echo  LIVE REAL-MONEY WRITES: NOT USED
echo.
echo  AUTH BOUNDARY:
echo  Testnet uses MEXC_DEMO_WEB_TOKEN only
echo  MEXC_WEB_TOKEN is NOT required by this launcher
echo.
echo  ENTRY:
echo  baseline residual ^>= 8bps and strength ^>= 3x unchanged
echo  signal gate / arrival economics / sizing / IOC unchanged
echo.
echo  LATENCY:
echo  scanner may wait only BEFORE trading mode
echo  scanner stops before trading starts
echo  no synthetic RTT / no fixed execution sleep
echo  confirmed fill -^> immediate position management
echo.
echo  TESTNET FEES:
echo  fee scope is selectable before scan
echo  fees never block trading when ALL is selected
echo  actual DEMO_FEES and DEMO_NET come from exchange fills
echo.
echo  REAL/LIVE RULE REMAINS STRICT:
echo  future real trading must require confirmed maker=0 AND taker=0
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

.venv\Scripts\python.exe -m mexc_tick_scalper.testnet_app ^
  --discovery-top 10 ^
  --scan-seconds 30 ^
  --profit-runner-arm-bps 5 ^
  --target-closed-trades 100 ^
  --session-seconds 86400 ^
  --max-signals 3000 ^
  --testnet-output persistent_end2end_TESTNET_SELECTED_PROFIT_HOLD.csv 2>"%LOG_FILE%"

set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" goto :failed

echo.
echo Testnet app finished normally. Exit code: %RC%
echo Error log: %CD%\%LOG_FILE%
pause
exit /b 0

:failed
set "RC=%ERRORLEVEL%"
if "%RC%"=="0" set "RC=2"
echo.
echo ============================================================
echo TESTNET APP FAILED. Exit code: %RC%
echo Error log: %CD%\%LOG_FILE%
echo ============================================================
if exist "%LOG_FILE%" (
  echo.
  type "%LOG_FILE%"
)
echo.
pause
exit /b %RC%
