@echo off
setlocal
cd /d "%~dp0"
call .venv\Scripts\activate.bat

echo ============================================================
echo  DIRECT BINANCE -^> MEXC TESTNET LATENCY-ARB PRODUCT
echo  leader=LIVE Binance / follower+execution=MEXC Testnet
echo  no artificial pre-submit sleep / no LIVE-MEXC mirror
echo ============================================================

.venv\Scripts\python.exe -m mexc_tick_scalper.testnet_latency_arb_product ^
  --target-closed-trades 100 ^
  --session-seconds 86400 ^
  --max-signals 100000 ^
  --risk-max-leverage 10 ^
  --min-liq-distance-bps 500 ^
  --emergency-liq-distance-bps 300 ^
  --max-adverse-roe-pct 8 ^
  --risk-poll-ms 100 ^
  --min-latency-profile-samples 4 ^
  --min-latency-survival-rate 0.60 ^
  --latency-safety-ms 50 ^
  --min-profit-reserve-bps 2

pause
endlocal
