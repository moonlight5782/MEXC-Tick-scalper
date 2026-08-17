# MEXC Tick Scalper — canonical handoff

Last updated: 2026-08-18

This file is the **single source of truth** for the active development branch `testnet-known-good-v1`.

## 1. Objective

Build a MEXC Futures tick/lead-lag scalper that uses real-time Binance USD-M + MEXC market data to detect short-lived MEXC lag, while validating execution on **MEXC Testnet only** before any live rollout.

The current task is **not strategy research**. The alpha/entry/exit logic already has a known-good reference. Current work is execution fidelity, partial IOC behavior, liquidation safety, emergency handling, and measurement of real Testnet results.

## 2. Immutable known-good reference

Known-good branch:

- `known-good-100trade-372c3b2`
- commit `372c3b286eb82aa4b87d806999f8db47173a2b3e`

That code completed the validated LIVE-data paper run:

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

A second independent run later reached 57 entries before interruption with 48/8/1, 84.2% WR, PF 15.143, +109.1505 USDT.

**Do not rewrite or re-optimize this strategy while working on Testnet execution.** If a Testnet run behaves differently, first investigate universe availability, exchange execution, latency, fees, position reconciliation, Testnet basis, contract precision, liquidity, or risk handling.

## 3. Known-good strategy invariants

The active Testnet implementation must preserve the known-good behavior:

- target requested notional: 10,000 USDT
- min signal residual: 8 bps
- min signal strength: 3.0x
- min residual retention: 60%
- min Binance impulse retention: 75%
- IOC cross: <= 1.0 bps
- max entry slippage: <= 1.0 bps
- min actual filled notional: 50 USDT
- executable edge must beat round-trip execution cost by >= 2.0 bps and >= 1.5x cost
- no pyramiding
- no martingale
- no averaging down
- no top-up after partial IOC
- no retry/chase merely to reach requested notional

Frozen exit decision order/semantics:

1. `mid_adverse_cut`
2. `leader_retrace`
3. `residual_reversal`
4. `mexc_catchup_convergence`
5. `no_progress`
6. `positive_trailing_stop`
7. `timeout`

Do not replace this with the older staged/hybrid exit policy.

## 4. IOC contract — critical

The intended behavior is a **single IOC attempt**.

Example:

- strategy requests 10,000 USDT
- only ~2,000 USDT is executable inside the IOC price limit at arrival
- MEXC fills ~2,000 USDT
- unfilled ~8,000 USDT is cancelled by IOC semantics
- strategy manages **only the actual ~2,000 USDT Testnet position**

There must be no second order to fill the remainder.

Source of truth after submit:

1. Testnet order `dealVol`
2. Testnet `open_positions` / actual `holdVol`
3. actual Testnet entry price
4. actual Testnet entry fee

If no remote position appears, count `NO FILL`; never create a paper position.

## 5. Active Testnet architecture

Active branch: `testnet-known-good-v1`.

Primary launcher:

- `start_testnet_known_good_v1.bat`

Primary runner:

- `src/mexc_tick_scalper/testnet_known_good_risk.py`

Helper/direct Testnet adapter:

- `src/mexc_tick_scalper/testnet_known_good_v1.py`

Execution adapter:

- `src/mexc_tick_scalper/web_execution.py`

Known-good strategy engine/dependencies are inherited from the 372c3b2 code path, especially:

- `prelive_persistent_ioc_shadow_v2.py`
- `prelive_persistent_ioc_shadow.py`
- `prelive_persistent_catchup_shadow.py`
- `lead_lag_strategy.py`
- `microspread.py`
- `microspread_feed.py`

### Market data

Alpha inputs:

- Binance USD-M LIVE public market data
- MEXC LIVE public market data

Execution:

- MEXC Futures Testnet only (`futures.testnet.mexc.com`)

The Testnet order must use the **same symbol** as the signal. No BTC proxy and no substitute symbol.

Universe for Testnet validation is constrained by same-symbol availability on Binance USD-M + LIVE MEXC + MEXC Testnet. This is an execution limitation, not a strategy optimization.

## 6. Testnet credentials and hard safety boundary

Use only:

- `MEXC_DEMO_WEB_TOKEN`

Do not paste credentials into chat, logs, docs, commits, or tests.

The Demo execution adapter must remain hard-bound to:

- `futures.testnet.mexc.com`

No active Testnet runner may send order writes to LIVE MEXC.

## 7. Order precision and contract metadata

Before Testnet submit:

- price must respect `priceUnit` / `priceScale`
- order volume must respect `contractSize`
- volume must respect `volUnit` / `volScale`
- volume must respect `minVol` / `maxVol`

Past failure to normalize these fields produced MEXC error `2015 Price or quantity precision error`.

Do not work around precision errors by changing strategy thresholds.

## 8. Realistic latency model

Known-good paper validation used measured MEXC private RTT and evaluated entry against the LIVE arrival-time book.

Testnet introduces real request latency. Do not accidentally apply a full synthetic RTT and then add a full real network RTT again.

The Testnet runner records:

- known-good/live RTT target
- Testnet private RTT
- pre-submit compensation wait
- signal-to-submit latency
- POST response latency
- order confirmation latency

When investigating lower performance, latency mismatch is one of the first things to inspect.

## 9. Risk and liquidation layer

Risk safeguards are an execution layer and must not silently alter alpha thresholds.

Current launcher defaults:

- max Testnet leverage: 10x
- minimum initial liquidation distance: 500 bps
- emergency liquidation distance: 300 bps
- emergency adverse ROE: -8%
- risk polling: 200 ms
- max Testnet-vs-LIVE basis: 50 bps

Why leverage is capped: previous liquidation replay showed extremely small liquidation buffers at exchange maximum leverage, e.g. about 48 bps for BANK at 100x and about 38 bps for ETHFI at 125x. Those settings are unsuitable for a latency-sensitive execution test.

After entry, remote Testnet position data is authoritative. Record/use:

- actual leverage
- actual liquidation price if returned by MEXC
- actual position quantity
- actual entry price
- actual fees

Emergency protections must close reduce-only and must not reverse the position.

Emergency reasons include:

- insufficient liquidation buffer immediately after fill
- liquidation buffer breached while holding
- adverse ROE failsafe
- repeated risk-monitor/API failures while a position is open

The strategy's normal exits should usually fire well before emergency protection. Emergency protection is a final technical safety net.

## 10. PnL accounting

Primary Testnet result must use **real Testnet execution**:

- actual entry price
- actual exit price
- actual filled quantity
- entry fee
- exit fee

Report at least:

- net PnL after both Testnet fees
- zero-fee counterfactual separately
- W/L/F
- PF in USDT
- actual fill ratio
- actual filled notional
- hold time
- exit reasons
- no-fill count
- rejected/aborted entries
- emergency exit count/reasons

Do not mix paper PnL with Testnet PnL in the primary result.

## 11. What counts as a valid Testnet trade

A trade exists only if all are true:

1. known-good strategy produced a valid signal
2. same symbol exists on Testnet and has a usable book
3. IOC submit was accepted
4. Testnet reports actual fill / remote position
5. position is managed from actual Testnet quantity
6. close order is accepted and remote position is actually reduced/removed
7. both entry and exit fees are included in net result

`SIGNAL` or a virtual `ENTRY` alone is not a Testnet trade.

## 12. Acceptance criteria for the current phase

Do not call Testnet adaptation successful until the following are demonstrated in logs/account history:

- real `DEMO ENTRY` orders appear in MEXC Testnet history
- same-symbol execution: signal symbol == Testnet order symbol
- a deliberately observed partial IOC shows requested ~10k but actual fill below 10k, with no top-up order
- actual remote position quantity matches managed quantity
- real reduce-only exits close positions reliably
- no residual position remains after normal exit, emergency exit, Ctrl+C cleanup, or exception cleanup
- liquidation distance is recorded for every accepted position when available
- risk failsafe can close a position without opening an opposite position
- actual Testnet fees are included in reported PnL
- at least 100 real closed Testnet trades are collected before comparing profitability with the known-good paper benchmark

Do not guarantee profitability. The goal is to preserve the known-good alpha and measure how much execution, fees, Testnet quality, latency, and risk controls change realized performance.

## 13. Current known limitations / investigations

- MEXC Testnet symbol universe is much smaller than the historical LIVE universe.
- Testnet liquidity and price path can differ materially from LIVE MEXC.
- Same-symbol Testnet restriction may reduce signal frequency.
- Testnet fees can differ from the production zero-fee target; therefore both real-fee and zero-fee-counterfactual PnL should be reported.
- Testnet basis vs LIVE must be monitored so bad Demo pricing is not mistaken for alpha failure.
- CI status has not reliably appeared for every branch commit; do not claim tests passed unless actually executed or GitHub checks report success.

## 14. Development rules for the next agent

1. Start by reading this file and `README.md`.
2. Never modify `known-good-100trade-372c3b2`.
3. Do not change strategy thresholds to fix Testnet execution problems.
4. Before editing a file, fetch current branch version/SHA.
5. Keep Testnet writes physically isolated from LIVE hosts.
6. Never expose `MEXC_DEMO_WEB_TOKEN`.
7. Do not introduce proxy-symbol execution.
8. Do not reintroduce stdout mirroring/paper-child architecture.
9. Do not reintroduce automatic MEXC max leverage.
10. Preserve single-attempt partial IOC semantics.
11. Verify remote position after every entry and close.
12. On shutdown/error, attempt reduce-only flatten and verify no residual.
13. Add regression tests for every execution/risk bug fixed.
14. Prefer small changes; compare against 372c3b2 behavior after each meaningful change.
15. Do not claim a real Testnet trade unless Testnet order/position evidence exists.

## 15. Files that are historical, not the active strategy

Many `demo_*`, older live runners, scanners, and diagnostics remain because they contain reusable exchange adapters, test fixtures, or research utilities. They are **not authoritative launchers**.

The only supported user-facing launchers for the current stage are:

- `start_live_shadow.bat` — known-good LIVE-data paper validation
- `start_testnet_known_good_v1.bat` — active real Testnet validation

If a historical module is not imported by active code, it may be moved to an archive or removed in a later dedicated dependency-cleanup PR. Do not mass-delete Python modules without checking imports/tests first.

## 16. Immediate next work

Priority order:

1. Run `start_testnet_known_good_v1.bat` and capture startup through first accepted IOC.
2. Confirm order appears in MEXC Testnet account history.
3. Confirm partial-fill semantics from real `dealVol/holdVol`.
4. Confirm liquidation price/distance logging and leverage <= configured cap.
5. Confirm normal strategy exit really removes remote position.
6. Confirm emergency exit path on a controlled Testnet scenario.
7. Accumulate real Testnet closed trades and compare:
   - signal frequency
   - fill ratio
   - median notional
   - hold time
   - fees
   - net PnL
   - zero-fee counterfactual
   - exit distribution
   against the known-good 372c3b2 benchmark.
8. Only after execution fidelity is established should performance/risk parameters be evaluated. Any proposed alpha change requires a new independent validation and must never overwrite the known-good reference.
