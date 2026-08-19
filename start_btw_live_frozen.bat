@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo ERROR: .venv\Scripts\python.exe not found.
  pause
  exit /b 1
)

call .venv\Scripts\activate.bat

echo ============================================================
echo  BTW_USDT ONLY - REAL MONEY LIVE EXECUTION
echo  LIVE Binance + LIVE MEXC market data
echo  REAL MEXC ORDERS
echo  target notional: 10 USDT
echo  leverage: 1x
echo  max cycles: 10
echo  session loss kill-switch: 2 USDT
echo  frozen entry gates: residual ^>= 8bps, strength ^>= 3x
echo ============================================================
echo.
echo Required environment:
echo   MEXC_WEB_TOKEN=your current LIVE MEXC web-session token
echo   MEXC_LIVE_WRITE=YES
echo.
set /p CONFIRM=Type LIVE BTW to enable real-money BTW orders: 
if /I not "%CONFIRM%"=="LIVE BTW" (
  echo Cancelled.
  pause
  exit /b 3
)

powershell -NoProfile -Command "$p = Start-Process -FilePath '%CD%\.venv\Scripts\python.exe' -ArgumentList '-m','mexc_tick_scalper.btw_live_frozen_execution','--confirm-live','LIVE','--target-notional-usdt','10','--leverage','1','--max-cycles','10','--max-session-loss-usdt','2','--session-seconds','3600','--trade-csv','btw_live_frozen_trades.csv' -Priority High -NoNewWindow -Wait -PassThru; exit $p.ExitCode"
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" echo Runner exited with code %RC%.
pause
exit /b %RC%
