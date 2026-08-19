# MEXC-Tick-scalper — canonical project state

Last updated: 2026-08-19

Repository: `moonlight5782/MEXC-Tick-scalper`

Active development branch: `persistent-end2end-latency-v1`

## 1. Product objective

Build a MEXC Futures latency/lead-lag scalper around the observed Binance -> MEXC follower lag. Binance USD-M is the leader; MEXC sometimes lags. The strategy opens only the MEXC leg when the residual is large and persistent enough to survive real execution delay and still leave positive executable edge after spread/depth/cost. It exits as MEXC catches up, the leader retraces, the residual reverses, progress fails, the positive trailing condition fires, or timeout is reached.

This is directional latency trading, not risk-free two-leg arbitrage.

## 2. Immutable validated alpha reference

Known-good 100-trade paper reference:

- branch: `known-good-100trade-372c3b2`
- commit: `372c3b286eb82aa4b87d806999f8db47173a2b3e`

Frozen baseline reference:

- commit: `8a0bc6043385dbaf95ec8e77b93d91fd00a7f9e5`
- parameters: `src/mexc_tick_scalper/baseline_v1.py`

Validated exact 100-trade paper result:

- signals: 326
- entries: 100
- expired: 225
- nofill: 1
- W/L/F: 76/22/2
- WR: 76.0%
- PF_USDT: 13.610
- PnL: +390.1785 USDT
- median fill: 14.8%
- median filled notional: ~$1,477
- median hold: 90 ms
- exits: leader_retrace 23, mexc_catchup_convergence 65, no_progress 4, residual_reversal 8

A later independent run reached 57 entries before interruption: 48/8/1, WR 84.2%, PF 15.143, +109.1505 USDT.

These results proved alpha at arrival-book entry time, but the original v2 paper runner did not model a separate exit latency. Therefore they are not by themselves proof of full end-to-end executable profitability.

## 3. Frozen strategy invariants

Do not change these while validating baseline v1:

- requested notional: 10,000 USDT
- pair minimum signals: 4
- pair median lag lifetime: >= 300 ms
- pair survival rate: >= 50%
- pair median signal strength: >= 1.50x
- min signal residual: 8.0 bps
- min signal strength: 3.0x
- residual retention at arrival: >= 60%
- Binance impulse retention at arrival: >= 75%
- IOC cross: <= 1.0 bps
- max entry slippage: <= 1.0 bps
- min actual filled notional: 50 USDT
- executable residual must beat round-trip cost by >= 2.0 bps and >= 1.5x
- no pyramiding
- no martingale
- no averaging down
- one IOC attempt only
- partial fill accepted
- unfilled remainder cancelled / never topped up or chased

Frozen exit priority:

1. `mid_adverse_cut`
2. `leader_retrace`
3. `residual_reversal`
4. `mexc_catchup_convergence`
5. `no_progress`
6. `positive_trailing_stop`
7. `timeout`

The old sub-1-bps microspread strategy, staged trailing-stop branch and hybrid runners are research history, not the active product strategy.

## 4. Current canonical validation runner

Launcher:

- `start_persistent_end2end_shadow.bat`

Runner:

- `src/mexc_tick_scalper/persistent_end2end_shadow.py`

Current inputs:

- LIVE Binance USD-M public `bookTicker`
- LIVE MEXC public depth
- LIVE MEXC exact account fee state (maker=0 and taker=0)
- frozen persistent-pair lifetime profiles
- frozen baseline v1 strategy
- realtime private MEXC read-path latency measurement

The runner is read-only. It constructs no LIVE or Testnet order write.

## 5. Current end-to-end semantics

### Entry

1. frozen alpha emits a signal
2. current realtime latency is captured at signal time
3. entry is scheduled for signal + measured latency
4. at arrival, use current LIVE MEXC depth
5. require fresh exact 0/0 fee state
6. recheck residual and leader/impulse retention
7. simulate one marketable IOC within 1 bps
8. accept partial fill only
9. require >= $50 actual fill
10. require <= 1 bps entry slippage
11. require remaining executable edge to beat immediate round-trip cost by frozen reserve/ratio
12. manage only the actual filled quantity

### Exit

Frozen exit conditions decide *when* to request a close. Latency is measured again at `EXIT DECISION`.

After `EXIT DECISION`, the close becomes sticky. Binance/residual validity cannot delay it. The modeled order arrives after the captured exit latency; the simulation then uses MEXC depth only. If fresh MEXC depth is unavailable at the exact local scheduler instant, the runner records the actual modeled arrival separately and records any additional book-wait before fill.

This separation fixes the prior bug where an already-requested exit could wait for a valid Binance+MEXC alpha snapshot.

## 6. Honest lifecycle accounting

The canonical CSV records:

- signal_ms
- entry_scheduled_arrival_ms
- entry_arrival_ms
- entry_schedule_overrun_ms
- exit_decision_ms
- exit_scheduled_arrival_ms
- exit_arrival_ms
- exit_schedule_overrun_ms
- close_ms
- exit_book_wait_ms
- measured entry/exit latency
- latency sample age
- stale-exit fallback flag
- entry/exit prices
- fill ratio/notional
- PnL
- hold time
- signal-to-close
- exit reason

The previous implementation wrote `exit_arrival_ms = decision + modeled latency` even when the actual local processing happened later. That optimistic accounting has been removed.

## 7. Session boundary / survivorship fix

A deadline, max-signal limit or target boundary disables *new* signals. It does not discard an already accepted pending entry or open position.

The runner enters a DRAINING state and carries the lifecycle to a terminal close before FINAL REPORT. This prevents a losing open trade from disappearing merely because the session ended.

## 8. Realtime latency model

No fixed 650/350 ms production constants remain.

`src/mexc_tick_scalper/realtime_latency.py` continuously measures the current LIVE MEXC private request path. Rolling median/p75/p95 are retained for diagnostics, but the effective current value cannot be lower than:

- the latest completed private RTT
- an in-flight private request that has already taken longer than the rolling profile

This prevents a current 900 ms spike from being hidden by a historical p75 near 300 ms.

Important limitation: private GET RTT is still a transport-path proxy, not actual IOC matching-engine latency. It is useful for current network/session state but not sufficient as the final execution model.

## 9. Next execution-calibration layer

MEXC Testnet should be used strictly to calibrate execution mechanics and latency, not to validate LIVE alpha PnL.

A clean Testnet execution calibrator should measure:

- decision -> POST start
- POST start -> HTTP response
- POST start -> terminal IOC state
- POST start -> position visible
- close decision -> close POST
- close POST -> terminal close
- close POST -> position absent
- actual `dealVol`
- actual `dealAvgPrice`
- actual fees
- account/direction private `risk_limit` maxVol/maxLeverage
- liquidation price / risk state

Useful execution work exists in historical branches, but must be ported as components, not by adopting their strategy runners.

## 10. Historical branch roles

Do not use these as the product base:

- `main` / `feature/mexc-binance-live`: Demo/liquidation/execution experiments after frozen baseline
- `latency-arb-product-v1`: direct Binance -> MEXC Testnet follower experiment; Testnet market process is not LIVE MEXC alpha
- `testnet-frozen-latency-v1`: useful WS-cache/timing ideas but wrapper/monkeypatch architecture
- `testnet-known-good-v1`: older same-symbol Testnet adaptation from 372c3b2
- `agent/*`: research history preceding or diverging from frozen baseline
- `staged-trailing-stop`: different exit policy

Immutable references remain `372c3b2` and `8a0bc60`.

## 11. Execution components to port later

Useful pieces found during full branch audit:

- Testnet WS quote cache so no REST price lookup precedes IOC
- prewarm contract metadata so `_to_contract_vol` does not add hidden REST RTT
- correct Decimal price/volume normalization
- actual `dealVol`, `dealAvgPrice`, fees and position state as truth
- private `/private/account/risk_limit` per symbol/direction
- leverage setup outside signal-critical path
- 8819 capacity rejection disables that side; never chase/retry signal
- close from already-known position snapshot, then reconcile; avoid a private GET before every close
- exact reduce-only reconciliation / residual cleanup

## 12. Safety

No real-money writes during the current end-to-end validation phase.

Do not expose tokens or credentials. Testnet writes, when building the execution calibrator, must remain hard-bound to `futures.testnet.mexc.com` and use explicit user intent.

## 13. Testing

CI workflow: `.github/workflows/ci.yml`, Python 3.11, `pytest -q`.

Never claim tests passed unless a local run or GitHub Actions confirms it. The current branch is not protected by mandatory status checks, so every engineering change must explicitly verify test status before being called release-ready.
