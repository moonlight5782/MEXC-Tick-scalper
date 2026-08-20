# Engineering instructions for MEXC-Tick-scalper

Read this file, `PROJECT_STATE.md`, and the current active launcher/entrypoint before changing strategy, execution, latency, or repository structure.

## Canonical development line

Active development branch: `persistent-end2end-latency-v1`.

Do not create another branch for a small fix. Keep this branch focused until the current Testnet product is structurally clean and validated end-to-end.

Historical strategy references:

- frozen successful shadow baseline commit: `8a0bc6043385dbaf95ec8e77b93d91fd00a7f9e5`
- frozen parameters: `src/mexc_tick_scalper/baseline_v1.py`

The frozen commit is a behavioral reference for the original signal/entry/defensive strategy. It is NOT the final product specification: current Testnet work additionally requires real network/exchange latency, immediate post-fill management, a post-fill signal guard, and Profit Hold for winners.

## Mandatory workflow before every material code change

1. Read the current caller and all directly affected dependencies.
2. State what responsibility is being changed and what must remain invariant.
3. Check whether an existing function has unrelated side effects before reusing it.
4. Prefer extracting a narrow component over adding another wrapper/runner fork.
5. Make the smallest coherent change.
6. Re-check imports/callers after the change.
7. Run relevant tests, then the full suite before claiming completion.
8. Never say tests passed unless a real test run or GitHub Actions confirms it.

Do not patch by analogy without inspecting the actual active path.

## Architecture rule: compose blocks, do not mix responsibilities

Target flow:

```text
Configuration
  -> UniverseService
  -> LeadLagScanner
  -> PairSelector
  -> TradingSession
       -> SignalEngine
       -> EntryPolicy
       -> ExecutionAdapter
       -> PositionManager
       -> ExitPolicy / ProfitHoldPolicy
       -> RiskManager
       -> Reporter
```

Each component owns one responsibility and receives explicit dependencies.

Forbidden in the active path:

- wrapper-on-wrapper runner chains;
- multiple active launchers for the same workflow;
- module-level mutable state controlling an active session;
- hidden monkeypatches spread across modules;
- deep `from_env()` calls inside business logic;
- Testnet code accidentally constructing LIVE private auth;
- discovery functions that also submit orders;
- trading functions that rediscover/select pairs;
- helper functions that perform unrelated network requests as a side effect;
- copied runner files that differ only by thresholds.

A compatibility bridge to legacy code may exist temporarily, but it must be isolated in one place and removed as the corresponding component is extracted.

## Current product mode

Current order writes are MEXC Futures Demo/Testnet only.

Testnet:

- uses `MEXC_DEMO_WEB_TOKEN` only;
- execution host must be hard-locked to `futures.testnet.mexc.com`;
- may test fee-paying pairs when the user selects ALL;
- Demo fees are reporting/accounting data and do not weaken or block Testnet strategy entry in ALL mode;
- scanner uses LIVE Binance + LIVE MEXC public data for alpha/thesis;
- Testnet is execution/PnL telemetry, not the signal source.

Real/LIVE order writes remain disabled. Do not restore them without explicit approval and a separate safety review.

Future LIVE eligibility is strict and non-overridable:

```text
maker == 0 AND taker == 0
```

Non-zero fee or unknown fee means no real trade.

## Frozen entry invariants

Do not silently change:

- requested target notional: `10_000 USDT`;
- requested Testnet leverage intent: `200x`, capped by contract/account limits;
- minimum absolute residual: `8.0 bps`;
- minimum signal strength: `3.0x`;
- residual retention: `60%`;
- Binance impulse retention: `75%`;
- IOC cross: `<= 1.0 bps`;
- max entry slippage: `<= 1.0 bps`;
- minimum actual filled notional: `50 USDT`;
- executable residual must beat round-trip cost by `>= 2.0 bps` and `>= 1.5x cost`;
- one IOC attempt; accept partial fill; never chase/top up simply to force a trade;
- no pyramiding, martingale, or averaging down;
- LeadLagGate re-arm behavior.

`baseline_v1.py` is immutable. A strategy change requires a separately versioned configuration and benchmark.

## Discovery is not trading entry

Discovery must be broad enough to rank useful pairs without weakening actual entry.

Current intended discovery floor:

- `LeadLagGate.ready`;
- residual approximately `>= min_edge_bps` (baseline 2 bps);
- strength `>= pair_min_strength_ratio` (baseline 1.5x).

The scanner should separately count true `8 bps / 3x` hits.

Trading after pair selection still requires the frozen `8 bps / 3x` entry plus all arrival-economics, depth, IOC, slippage, freshness, and cost checks.

## Latency contract — critical

During Trading Mode, software-added delay is forbidden.

Allowed blocking time is only the unavoidable time for:

- current network transport;
- exchange/server processing;
- a network request that is genuinely necessary to know the order result.

Forbidden on the critical path:

- `time.sleep` / `asyncio.sleep`;
- synthetic/emulated RTT;
- fixed polling sleeps;
- intentional stability waits;
- redundant private verification before managing a confirmed fill;
- optional REST lookups that can be prewarmed/cached;
- logging/serialization work before submit that can be moved off the path.

Event-driven waiting is acceptable when market events wake immediately; a timeout may only be an idle heartbeat, not a mandatory per-signal delay.

Pre-trade discovery may sample/wait because it runs before Trading Mode.

## Confirmed fill -> immediate management

A confirmed entry fill already provides execution truth such as qty, average price, and order/position identity.

Do not block position management on `get_positions()` immediately after a confirmed fill.

Use `get_positions()` for startup reconciliation, residual close verification, stale-state recovery, and emergency reconciliation only.

Immediately after fill, run the post-fill guard against the freshest already-available LIVE market state. If the original edge collapsed/reversed or became invalid while the order was in flight, flatten immediately without an artificial wait.

## Position management / Profit Hold

There are two states:

1. position has never reached positive executable PnL;
2. position has reached positive executable PnL.

Before first positive executable PnL, keep original defensive behavior: a broken/losing thesis should be abandoned quickly.

Original defensive/lifecycle exits include:

1. `mid_adverse_cut`
2. `leader_retrace`
3. `residual_reversal`
4. `mexc_catchup_convergence`
5. `no_progress`
6. `positive_trailing_stop`
7. `timeout`

On the first positive executable PnL, arm **Profit Hold**.

After Profit Hold is armed, ordinary thesis/lifecycle exits must not prematurely cut a winner:

- convergence;
- leader retrace;
- residual reversal;
- no-progress;
- ordinary timeout.

The normal winner exit is the ratcheting positive trailing stop.

Hard/emergency/exchange/forced cleanup protection remains active at all times. Profit Hold means “let winners run”, not “never close”.

The stop only ratchets upward and must never intentionally be set above the currently executable PnL decision point. Realized fill may be worse because of network/slippage.

## Risk

Current intent:

- start logical bank: `100 USDT`;
- reserve at least `20%` equity;
- session max drawdown: `60%` from start;
- target notional remains `10_000 USDT`, capped by effective leverage and reserve logic.

Do not silently change sizing or session kill-switch semantics.

## Reporting

For Testnet distinguish at least:

```text
GROSS
DEMO_FEES
DEMO_NET
```

Record signal/submit/fill/manage/exit timestamps so actual latency can be decomposed without inserting artificial delays.

Useful metrics include:

- signal_to_submit_ms;
- submit_to_fill_ms;
- signal_to_fill_ms;
- fill_to_management_ms;
- exit_decision_to_submit_ms;
- exit_submit_to_fill_ms.

## Repository hygiene

The root should contain only active project/config/docs/launchers and deliberately curated research references.

Experimental generations should not remain mixed with active product modules forever. Before moving/deleting a legacy file:

1. check imports and test references;
2. extract any still-needed behavior into a named component;
3. update tests;
4. only then archive/delete the obsolete file.

Do not delete historical reference code merely because the filename looks old.

## Testing

Before saying a change is complete:

```bash
pip install -e . pytest
pytest -q
```

CI is `.github/workflows/ci.yml` on Python 3.11.

Minimum architecture/invariant coverage should include:

- Testnet never requires LIVE private auth;
- Demo host hard lock;
- discovery thresholds independent from 8/3 trading thresholds;
- 7.99 bps rejected / 8.00+ allowed if other gates pass;
- 2.99x rejected / 3.00x+ allowed if other gates pass;
- duplicate impulse rejected until re-arm;
- no software sleep on trading critical path;
- confirmed fill starts management without blocking `get_positions()`;
- post-fill guard can flatten a decayed signal immediately;
- Profit Hold arms only after positive executable PnL;
- ordinary thesis exits are suppressed after Profit Hold;
- hard safety remains active;
- runtime state cannot leak between sessions;
- shutdown/residual cleanup works.
