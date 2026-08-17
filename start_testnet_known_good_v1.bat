@echo off
setlocal
cd /d "%~dp0"
call .venv\Scripts\activate.bat

REM Known-good 372c3b2 strategy, REAL same-symbol MEXC Testnet execution.
REM Strategy/alpha thresholds are unchanged.
REM IOC requests $10k once; actual Testnet partial fill is managed, remainder cancels, NO top-up/chase.
REM Testnet position/dealVol/fees/liquidationPrice are execution source of truth.
REM Independent safeguards: leverage cap, liquidation buffer, adverse-ROE emergency exit, risk-monitor fail-safe.
REM No LIVE order writes.

.venv\Scripts\python.exe -m mexc_tick_scalper.testnet_known_good_risk ^
  --target-closed-trades 100 ^
  --session-seconds 86400 ^
  --max-signals 3000 ^
  --risk-max-leverage 10 ^
  --min-liq-distance-bps 500 ^
  --emergency-liq-distance-bps 300 ^
  --max-adverse-roe-pct 8 ^
  --risk-poll-ms 100 ^
  --max-risk-poll-failures 5 ^
  --max-testnet-basis-bps 50

pause
