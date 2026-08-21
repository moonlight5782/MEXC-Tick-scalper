# Engineering instructions for MEXC-Tick-scalper

Read these files before any material change:

1. `AGENTS.md`
2. `PROJECT_STATE.md`
3. `CODEX_HANDOFF_20260821.md`
4. `CHAT_HISTORY.md`
5. the current active launcher/entrypoint and directly affected modules/tests

Repository: `moonlight5782/MEXC-Tick-scalper`

Active development branch: `persistent-end2end-latency-v1`.

Current branch state at the 2026-08-21 documentation refresh is based on HEAD `fa230467cd15efea9cdc4ffb0736e22ce6d19985` plus later documentation-only commits.

Do not create another branch for a small fix. Keep this branch focused until the current Testnet product is structurally clean and validated end-to-end.

## Source-of-truth order

When historical documents conflict, use this priority:

1. active branch code;
2. regression/architecture tests on the active branch;
3. `CODEX_HANDOFF_20260821.md` for current intent/frozen invariants;
4. `PROJECT_STATE.md` for current engineering state;
5. `DEVELOPER_HANDOFF_20260816.md` for older measured results/history;
6. `CHAT_HISTORY.md` / linked ChatGPT project for decision context;
7. old commits as forensic references only.

Historical strategy references:

- known-good 100-trade paper reference: `372c3b286eb82aa4b87d806999f8db47173a2b3e`
- frozen successful shadow baseline: `8a0bc6043385dbaf95ec8e77b93d91fd00a7f9e5`
- frozen parameters: `src/mexc_tick_scalper/baseline_v1.py`

The frozen references are behavioral references, not the full current product specification. The current structured Testnet path additionally requires real network/exchange latency, confirmed-fill immediate management, a post-fill guard, Profit Hold and exact Demo close/reconciliation telemetry.

## Non-negotiable refactor rule

The owner does NOT want a rewritten bot. The task is to split the existing working bot into independent blocks while preserving the same local startup, `.env`, Testnet flow and strategy behavior 1:1.

Refactor protocol:

1. characterize existing behavior with tests;
2. extract exactly one responsibility;
3. preserve network-call ordering and side effects;
4. run focused tests and then full suite;
5. user performs normal local Testnet run with existing `.env`/launcher;
6. only after behavior is shown unchanged, continue to the next block.

Never mix architectural extraction with strategy tuning.

## Mandatory workflow before every material code change

1. Read the current caller and all directly affected dependencies.
2. State what responsibility is being changed and what must remain invariant.
3. Check whether an existing helper has unrelated side effects before reusing it.
4. Prefer extracting a narrow component over another wrapper/runner fork.
5. Make the smallest coherent change.
6. Re-check imports/callers after the change.
7. Run relevant tests, then full suite before claiming completion.
8. Never say tests passed unless a real local test run or GitHub Actions confirms it.

Do not patch by analogy without inspecting the actual active path.

## Target architecture

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
            -> ExitPolicy
            -> ProfitHoldPolicy
       -> RiskManager / risk state
       -> Reporter
```

Names may evolve, but responsibility boundaries must remain explicit.

Forbidden in the active path:

- wrapper-on-wrapper runner chains;
- multiple active launchers for the same workflow;
- module-level mutable state controlling an active session;
- hidden monkeypatches;
- deep `from_env()`/`load_dotenv()` calls inside business logic;
- Testnet code constructing LIVE private auth;
- discovery that submits orders;
- trading code that rediscover/selects pairs;
- helpers that perform unrelated network requests as side effects;
- copied runners differing only by thresholds;
- moving behavior into a new class while keeping duplicate old behavior active.

## Current structured Testnet composition

Already separated:

- `testnet/config.py` — `.env` bootstrap and explicit Demo read/write dependencies;
- `testnet/universe.py` — public/Testnet contract universe;
- `testnet/scanner.py` — discovery only;
- `testnet/selector.py` — console pair/fee-scope selection only;
- `testnet/session.py` — composition/orchestration boundary;
- `testnet/execution.py` — Demo execution, confirmed-fill conversion and close/reconciliation;
- `testnet/risk.py` — logical bank/leverage/sizing/IOC price rounding;
- `testnet/exit_policy.py` — exit decisions;
- `testnet/profit_hold.py` — winner state and ratcheting positive stop;
- `testnet/reporting.py` — stats and CSV timing/PnL telemetry;
- `testnet/position_manager.py` — confirmed-position lifecycle through terminal close.

Current major coupling still remaining:

- `testnet/trading_engine.py::_try_open()` still mixes signal decision, entry policy/economics, sizing, IOC submission, confirmed-fill state construction and post-fill guard.

Next architecture work should characterize `_try_open()` first, then extract entry responsibilities mechanically without changing behavior.

## Current product mode

Current order writes are MEXC Futures Demo/Testnet only.

Testnet:

- uses `MEXC_DEMO_WEB_TOKEN` only;
- execution host must be hard-locked to `futures.testnet.mexc.com`;
- local writes require explicit `MEXC_DEMO_WRITE=YES`;
- may test fee-paying pairs when the user selects ALL;
- Demo fees are reporting/accounting data and do not weaken strategy entry in ALL mode;
- scanner/trading thesis uses LIVE Binance + LIVE MEXC public data;
- Testnet is execution/PnL/latency telemetry, not the alpha source.

Real/LIVE order writes remain disabled. Do not restore them without explicit approval and a separate safety review.

Future LIVE eligibility remains strict:

```text
maker == 0 AND taker == 0
```

Unknown or non-zero fee means no real trade.

## Frozen entry invariants

Do not silently change:

- requested target notional: `10_000 USDT`;
- requested Testnet leverage intent: `200x`, capped by contract/account limits;
- minimum absolute residual: `8.0 bps`;
- minimum signal strength: `3.0x`;
- residual retention reference: `60%`;
- Binance impulse retention reference: `75%`;
- IOC cross: `<= 1.0 bps`;
- max actual entry slippage: `<= 1.0 bps`;
- minimum actual filled notional: `50 USDT`;
- executable residual must beat round-trip cost by `>= 2.0 bps` and `>= 1.5x cost`;
- one IOC attempt;
- partial fill accepted;
- never chase/top up the remainder;
- no pyramiding, martingale or averaging down;
- LeadLagGate re-arm behavior.

`baseline_v1.py` is immutable. A strategy change requires separately versioned configuration and a new benchmark.

## Discovery is not trading entry

Discovery must remain broader than the strict entry gate so useful pairs can be ranked.

Current intended discovery reference:

- `LeadLagGate.ready`;
- residual around baseline `2 bps`;
- strength around baseline `1.5x`;
- scanner separately counts actual `8 bps / 3x` hits.

After pair selection, actual trading still requires strict 8/3 plus arrival economics, depth, IOC, slippage, freshness and cost checks.

## Critical network-ordering contract

Preserve the effective order of the entry path:

```text
LIVE snapshot / gate / cheap economics
-> sizing / virtual depth economics
-> Demo best-price lookup
-> one Demo IOC submit
-> exchange-confirmed fill
-> immediate management state
-> immediate post-fill guard
```

Do not add an extra private `get_positions()` between confirmed fill and management.

During Trading Mode software-added delay is forbidden.

Forbidden on the critical path:

- `time.sleep` / `asyncio.sleep`;
- synthetic/emulated RTT;
- fixed polling sleeps;
- intentional stability waits;
- redundant private verification before managing a confirmed fill;
- optional REST lookups that can be prewarmed/cached.

Event-driven waiting is acceptable when market events wake immediately; a timeout may only be an idle heartbeat, not a mandatory signal delay.

## Confirmed fill -> immediate management -> post-fill guard

A confirmed fill provides qty/avg price/order/position truth. Build `PositionSnapshot` directly from the fill and begin lifecycle management immediately.

Then evaluate the freshest already-available LIVE state. If alpha collapsed/reversed, the snapshot/book is invalid/stale, actual fill is below minimum, or actual slippage exceeds the frozen limit, flatten immediately.

Do not weaken or postpone this guard.

## PositionManager boundary

`testnet/position_manager.py` owns only a confirmed position from fill until terminal close.

It may:

- evaluate current position lifecycle state;
- arm/update Profit Hold;
- invoke ExitPolicy;
- request full close through TestnetExecutionAdapter;
- forward terminal close telemetry to TradeReporter.

It must NOT own:

- discovery;
- PairSelector;
- LeadLagGate entry generation;
- requested entry notional calculation;
- `open_ioc()`.

`tests/test_position_manager_regression.py` currently protects at least:

- emergency `mid_adverse_cut` happens before Demo quote lookup;
- first positive executable PnL arms Profit Hold without forcing an immediate close;
- close reason/fill/reconciliation/attempt telemetry is forwarded to reporter.

## Position management / Profit Hold

Before first positive executable PnL, preserve defensive thesis exits.

Exit semantics include:

1. `mid_adverse_cut`
2. `leader_retrace`
3. `residual_reversal`
4. `mexc_catchup_convergence`
5. `no_progress`
6. `positive_trailing_stop`
7. `timeout`

On the first positive executable PnL, arm Profit Hold.

After Profit Hold is armed, ordinary thesis/lifecycle exits must not prematurely cut a winner:

- convergence;
- leader retrace;
- residual reversal;
- no-progress;
- ordinary timeout.

The normal winner exit becomes the ratcheting positive trailing stop. Hard/emergency/exchange/forced cleanup protection remains active at all times.

Current `ProfitHoldPolicy` ratchet:

- first positive -> armed;
- initial floor = `min(0.10 bps, move * 0.5)`;
- peak >= 3 -> stop >= +0.5 bps;
- peak >= 5 -> stop >= +2.0 bps;
- peak >= 6 -> stop >= `peak - max(0.1, distance_bps)`.

Do not restore the obsolete `--profit-runner-arm-bps` behavior.

## Risk

Current intent:

- start logical bank: `100 USDT`;
- reserve at least `20%` equity;
- max session drawdown: `60%` from start;
- target notional remains `10_000 USDT`, capped by effective leverage/reserve logic;
- one position at a time.

Do not silently change sizing or kill-switch semantics.

## Reporting

For Testnet distinguish:

```text
GROSS
DEMO_FEES
DEMO_NET
```

Record signal/submit/fill/manage/exit timestamps so latency can be decomposed without inserting delays.

Critical metrics include:

- signal_to_submit_ms;
- submit_to_fill_ms;
- signal_to_fill_ms;
- fill_to_management_ms;
- exit_decision_to_submit_ms;
- exit_submit_to_fill_ms;
- exit_reconcile_ms;
- close_attempts.

## Repository hygiene

The root should contain active project/config/docs/launchers and curated historical references only.

Before moving/deleting a legacy file:

1. check imports/tests;
2. extract still-required behavior;
3. update tests;
4. then archive/delete obsolete code.

Do not delete historical reference code merely because its filename looks old.

## Testing

Before saying a change is complete:

```bash
pip install -e . pytest
pytest -q
```

CI is `.github/workflows/ci.yml` on Python 3.11, but the branch currently has no mandatory protected status checks.

Never claim tests passed unless they actually ran.

After a material architecture change affecting the Testnet path, the owner performs a normal local Testnet run with the existing `.env` and launcher. Do not replace that workflow with GitHub Actions.

Minimum regression/invariant coverage should include:

- Testnet never requires LIVE private auth;
- Demo host hard lock;
- discovery thresholds independent from strict 8/3 trading thresholds;
- 7.99 bps rejected / 8.00+ allowed if other gates pass;
- 2.99x rejected / 3.00x+ allowed if other gates pass;
- duplicate impulse rejected until re-arm;
- no software sleep on trading critical path;
- confirmed fill starts management without blocking `get_positions()`;
- post-fill guard can flatten a decayed signal immediately;
- Profit Hold arms only after positive executable PnL;
- ordinary thesis exits are suppressed after Profit Hold;
- hard safety remains active;
- PositionManager never owns discovery/entry submission;
- runtime state cannot leak between sessions;
- shutdown/residual cleanup works.
