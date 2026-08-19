# Codex instructions for MEXC-Tick-scalper

Read this file and `PROJECT_STATE.md` before changing strategy, execution, or latency code.

## Canonical development line

Active development branch: `persistent-end2end-latency-v1`.

Immutable strategy references:

- known-good 100-trade commit: `372c3b286eb82aa4b87d806999f8db47173a2b3e`
- frozen validated baseline: `8a0bc6043385dbaf95ec8e77b93d91fd00a7f9e5`
- frozen parameters: `src/mexc_tick_scalper/baseline_v1.py`

Do not rebuild the strategy from `main`, old `agent/*`, `staged-trailing-stop`, `testnet-known-good-v1`, `testnet-frozen-latency-v1`, or `latency-arb-product-v1`. Those branches are research/execution history only. Individual infrastructure ideas may be ported after review, but their strategy runners are not the product source of truth.

## Primary objective

Validate and then execute the frozen persistent Binance -> MEXC lag strategy with honest end-to-end latency and current executable MEXC depth.

The frozen strategy is not the old sub-1-bps microspread experiment. Do not silently lower thresholds to increase trade count.

Key frozen invariants include:

- target requested notional: 10,000 USDT
- min absolute residual: 8.0 bps
- min signal strength: 3.0x
- min residual retention: 60%
- min Binance impulse retention: 75%
- IOC cross: <= 1.0 bps
- max entry slippage: <= 1.0 bps
- min actual filled notional: 50 USDT
- executable residual must beat round-trip cost by >= 2.0 bps and >= 1.5x cost
- one IOC attempt; accept partial fill; never top up/chase the remainder
- no pyramiding, martingale, averaging down, or retry merely to force a trade

Frozen exit priority:

1. `mid_adverse_cut`
2. `leader_retrace`
3. `residual_reversal`
4. `mexc_catchup_convergence`
5. `no_progress`
6. `positive_trailing_stop`
7. `timeout`

Do not replace these exits with staged/hybrid trailing logic while validating baseline v1.

## Current validation architecture

Canonical launcher:

- `start_persistent_end2end_shadow.bat`

Canonical runner:

- `src/mexc_tick_scalper/persistent_end2end_shadow.py`

Current validation uses:

- LIVE Binance public market data as leader
- LIVE MEXC public depth as follower/executable book
- current LIVE MEXC exact account maker=0 / taker=0 eligibility
- frozen persistent-pair profile selection
- frozen alpha/entry/exit thresholds
- realtime measured MEXC private transport latency
- arrival-time LIVE MEXC depth for entry and exit simulation
- no LIVE or Testnet order writes

Once an `EXIT DECISION` is made, the close path is sticky: it must never wait for Binance/residual validity again. Only MEXC execution/depth availability may affect simulated fill after the modeled arrival time.

A session/max-signal boundary disables new signals but must not discard an accepted pending entry or open position. Drain the lifecycle to a terminal close before reporting final PnL.

## Latency rules

No hard-coded production latency constants.

Realtime transport measurement is a read-only proxy, not proof of IOC matching-engine latency. The current estimator must not smooth away a current spike: the effective value is at least the latest completed private RTT and any already-longer in-flight private request.

Record modeled and actual local scheduling timestamps separately:

- signal time
- scheduled entry arrival
- actual entry processing arrival
- entry schedule overrun
- exit decision time
- scheduled exit arrival
- actual exit arrival
- exit schedule overrun
- close time / depth wait

Historical latency CSV is explicit replay only.

The next execution-calibration component should use MEXC Testnet strictly for execution telemetry (POST response, terminal IOC, position visibility, close terminal, position absent, dealVol, dealAvgPrice, fees, risk limits). Testnet price behavior must not be used to validate LIVE alpha PnL.

## LIVE / Testnet safety boundary

The current canonical shadow runner is structurally read-only and must not construct order writes.

Do not enable real-money order writes while baseline end-to-end profitability is still being validated. Testnet/Demo order writes may only be used in a dedicated execution calibrator with hard Testnet host checks and explicit user intent.

Never print, commit, or request secrets/tokens in chat.

## Execution components worth preserving later

When a real execution adapter is reconnected, preserve these proven infrastructure ideas without importing old strategy runners:

- cached WS best bid/ask on the critical path; no REST price lookup before IOC
- prewarmed contract metadata
- correct `priceUnit` / `priceScale` and `volUnit` / `volScale`
- account/direction private `risk_limit` capacity, not public `maxVol` as account capacity
- leverage/risk setup outside signal -> IOC path
- actual `dealVol`, `dealAvgPrice`, fees and position state as execution truth
- close from already-known position state, then reconcile; do not add a private GET before every close
- exact reduce-only reconciliation and bounded residual cleanup

## Testing

Before saying a change is complete:

```bash
pip install -e . pytest
pytest -q
```

CI is `.github/workflows/ci.yml` on Python 3.11. Do not claim tests passed unless a local run or GitHub Actions actually confirms it.

Never change `baseline_v1.py` in place. A future strategy change requires a separately named baseline and independent validation.
