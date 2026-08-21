# MEXC-Tick-scalper — canonical project state

Last updated: 2026-08-21

Repository: `moonlight5782/MEXC-Tick-scalper`

Active development branch: `persistent-end2end-latency-v1`

Primary current handoff: `CODEX_HANDOFF_20260821.md`

Chat/context index: `CHAT_HISTORY.md`

## 1. Product objective

Build a low-latency directional Binance -> MEXC Futures lead-lag scalper. Binance USD-M is the leader; MEXC sometimes lags. The strategy opens only the MEXC leg when the residual is large and persistent enough to survive real execution delay and still leave positive executable edge after spread/depth/cost.

This is directional latency trading, not risk-free two-leg arbitrage.

Current engineering objective is NOT to redesign the strategy. It is to split the existing working Testnet bot into independent components while preserving the same local startup, `.env`, signal/entry/exit semantics, network-call ordering and Demo/Testnet execution behavior.

## 2. Canonical references and measured history

Known-good 100-trade paper reference:

- commit: `372c3b286eb82aa4b87d806999f8db47173a2b3e`

Frozen successful shadow baseline reference:

- commit: `8a0bc6043385dbaf95ec8e77b93d91fd00a7f9e5`
- parameters: `src/mexc_tick_scalper/baseline_v1.py`

Validated exact historical 100-trade paper result:

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

These historical paper results proved alpha at arrival-book entry time, but did not by themselves prove full end-to-end executable profitability.

Older measured Demo/LIVE shadow results are preserved in `DEVELOPER_HANDOFF_20260816.md` and summarized in `CODEX_HANDOFF_20260821.md`. They remain evidence for strategy research history, not a license to replace the current structured strategy with the older Binance-impulse runner.

## 3. Current frozen strategy / execution invariants

Do not change during the architecture refactor:

- requested target notional: 10,000 USDT
- requested leverage intent: 200x, capped by contract/account limits
- min trading residual: 8.0 bps
- min trading signal strength: 3.0x
- discovery remains broader than trading entry
- residual retention reference: 60%
- Binance impulse retention reference: 75%
- IOC cross: <= 1.0 bps
- max actual entry slippage: <= 1.0 bps
- min actual filled notional: 50 USDT
- executable residual must beat round-trip cost by >= 2.0 bps and >= 1.5x
- one IOC attempt only
- partial fill accepted
- no chase/top-up of unfilled remainder
- no pyramiding
- no martingale
- no averaging down
- LeadLagGate re-arm behavior unchanged

Risk:

- logical start bank: 100 USDT
- reserve >= 20% equity
- max session drawdown: 60% from start
- one open position at a time

Frozen/current exit semantics:

1. `mid_adverse_cut`
2. `leader_retrace`
3. `residual_reversal`
4. `mexc_catchup_convergence`
5. `no_progress`
6. `positive_trailing_stop`
7. `timeout`

Profit Hold arms on the first positive executable PnL. After arm, ordinary thesis exits are suppressed; hard safety remains active and the positive trailing stop owns the normal winner exit.

## 4. Current product mode and local startup

The active product path is now the structured Testnet application, not the old read-only end-to-end shadow runner.

Runtime inputs/roles:

- LIVE Binance public market data: leader/alpha input
- LIVE MEXC public depth: follower/residual/spread/depth input
- MEXC Futures Demo/Testnet private path: order execution and PnL/latency telemetry only

Real/LIVE private order writes remain disabled.

Testnet execution:

- uses `MEXC_DEMO_WEB_TOKEN`
- host must be locked to `futures.testnet.mexc.com`
- local write unlock: `MEXC_DEMO_WRITE=YES`
- LIVE private credentials are not required for Testnet

The user launches locally with the existing `.env` and launcher/CLI. Do not replace this with a GitHub Actions trading workflow.

Console entry point:

```text
mexc-testnet = mexc_tick_scalper.testnet_app:main
```

Check `start_auto_testnet.bat` before changing startup behavior.

## 5. Current structured architecture

Current composition:

```text
Configuration
  -> UniverseService
  -> LeadLagScanner
  -> PairSelector
  -> TradingSession
       -> TestnetTradingEngine
            -> LeadLagGate / signal path
            -> entry logic (still too coupled)
            -> TestnetExecutionAdapter
            -> PositionManager
                 -> TestnetExitPolicy
                 -> ProfitHoldPolicy
            -> BankState / risk helpers
            -> TradeReporter
```

Already extracted/separated:

### `testnet/config.py`

- loads project `.env` at composition/bootstrap level
- constructs explicit readonly and trading Demo execution configs
- validates Demo environment/write lock/host

### `testnet/universe.py`

- builds public/Testnet contract universe
- receives execution dependency explicitly
- does not load env itself
- does not require LIVE private auth

### `testnet/scanner.py`

- lead-lag discovery only
- no execution responsibility

### `testnet/selector.py`

- fee-scope/pair selection only
- no trading/network side effects

### `testnet/session.py`

- composition/orchestration boundary
- legacy global monkeypatch bridge removed
- builds `TestnetTradingEngine`

### `testnet/execution.py`

- Demo/Testnet execution only
- hard Demo environment requirement
- no software-added polling sleep in the order-result path
- builds `PositionSnapshot` directly from confirmed fill
- close is submitted first, then residual state is reconciled

### `testnet/risk.py`

- logical bank
- requested/effective leverage
- target sizing/reserve logic
- Demo IOC price rounding

### `testnet/exit_policy.py`

- owns exit decisions through `ExitContext`
- emergency adverse protection remains active even after Profit Hold

### `testnet/profit_hold.py`

- owns winner state and ratcheting positive trailing stop

### `testnet/reporting.py`

- SessionStats
- CSV telemetry
- GROSS / DEMO_FEES / DEMO_NET
- latency decomposition and close accounting

### `testnet/position_manager.py`

Current extracted lifecycle component.

Owns a confirmed position from fill until terminal close:

- current LIVE position-state evaluation
- hard adverse cut
- Demo executable exit quote where required
- Profit Hold update
- ExitPolicy evaluation
- full close through execution adapter
- terminal telemetry to reporter

It deliberately does NOT own discovery, pair selection, requested entry sizing or `open_ioc()`.

## 6. Current critical entry path

`testnet/trading_engine.py::_try_open()` is the largest remaining coupled block.

It currently contains, in order:

1. current snapshot validation
2. LeadLagGate observation
3. strict signal-strength / absolute-residual gate
4. `TradeSignal` creation
5. arrival-entry economics
6. requested risk sizing
7. virtual IOC/depth economics
8. slippage and executable edge/cost gate
9. Demo best-price lookup
10. Demo IOC limit price normalization
11. one actual Demo `open_ioc()`
12. confirmed-fill -> `PositionSnapshot`
13. immediate timing/stats updates
14. `ActivePosition` construction
15. immediate post-fill LIVE guard
16. immediate close through PositionManager if guard fails

This ordering is part of the behavior contract. Do not casually reorder network requests while extracting components.

## 7. Latency contract

During Trading Mode no software-added delay is allowed.

Do not add:

- `time.sleep`
- `asyncio.sleep`
- synthetic RTT
- intentional stability waits
- fixed polling sleeps
- redundant private verification before confirmed-fill management

A confirmed fill should start management immediately. Do not block management on an extra `get_positions()` request.

Immediate post-fill guard uses the freshest already-available LIVE market state and must flatten immediately if the original alpha collapsed/reversed or became invalid while the order was in flight.

Hard adverse exit is evaluated from current LIVE state before a potentially slower Demo REST quote request.

## 8. PositionManager extraction status

Mechanical lifecycle extraction was performed in:

- `85ef91f5700185055e73d3498b75e4e418a5a2da` — `Delegate Testnet position lifecycle to PositionManager`

Architecture boundary guard:

- `0e8e36b6e4fa214bab3248383b396aae01b65661` — `Guard PositionManager architecture boundary`

Behavior characterization:

- `6dac76cec87ea5c6d922d12a9d6f62eeca1d4875` — `Characterize Testnet position lifecycle behavior`

`tests/test_position_manager_regression.py` currently fixes these critical behaviors:

1. `mid_adverse_cut` closes before Demo quote lookup
2. first positive executable PnL arms Profit Hold without forcing an immediate close
3. close reason, fill confirmation, reconciliation timestamp and attempt count reach the reporter

`tests/test_testnet_architecture.py` ensures PositionManager does not absorb discovery/entry submission and that TradingEngine delegates open-position lifecycle.

## 9. Post-fill guard

After confirmed IOC fill, the system immediately creates position-management state and then checks fresh LIVE state.

Immediate flatten reasons include:

- invalid live snapshot
- stale live book
- actual fill below minimum
- actual entry slippage above frozen max
- arrival residual reversed / remaining edge collapsed

Do not weaken or delay this guard during refactor.

## 10. Profit Hold current semantics

`ProfitHoldPolicy`:

- first positive executable PnL -> armed
- initial positive floor = `min(0.10 bps, move * 0.5)`
- peak >= 3 bps -> stop at least +0.5 bps
- peak >= 5 bps -> stop at least +2.0 bps
- peak >= 6 bps -> ratchet toward `peak - max(0.1, distance_bps)`

After arm, ordinary thesis exits are suppressed. Hard emergency protection always remains active.

Do not restore the obsolete `profit-runner-arm-bps` threshold.

## 11. Reporting / accounting

Current Testnet reporting distinguishes:

```text
GROSS
DEMO_FEES
DEMO_NET
```

CSV timing includes at least:

- signal_ms
- entry_submit_ms-derived timings
- entry_ms / confirmed fill
- management_start_ms
- exit_decision_ms
- exit_submit_ms
- exit_fill_ms
- exit_reconciled_ms
- signal_to_submit_ms
- submit_to_fill_ms
- signal_to_fill_ms
- fill_to_management_ms
- exit_decision_to_submit_ms
- exit_submit_to_fill_ms
- exit_reconcile_ms
- close_attempts
- exit_reason
- Profit Hold state

Do not merge Demo fee-adjusted PnL into gross alpha results.

## 12. Current refactor target

Next task is entry-path extraction from `_try_open()`.

Correct order of work:

### A. Characterize current `_try_open()` behavior first

Tests should cover at minimum:

- invalid/stale snapshot -> no signal/order
- gate not ready -> no order
- strength below 3x -> reject
- residual below 8 bps -> reject
- arrival residual reversed -> reject
- remaining edge too small -> reject
- requested sizing unchanged
- virtual IOC insufficient -> nofill/reject as currently defined
- max slippage failure -> reject
- executable cost/edge failure -> reject
- Demo best-price lookup occurs only after cheaper LIVE/economic gates
- exactly one `open_ioc()` attempt
- no actual fill -> no ActivePosition
- confirmed partial fill accepted
- confirmed fill begins management without mandatory position GET
- all post-fill guard failure paths close immediately
- successful path creates identical `ActivePosition` state/timings

### B. Extract one responsibility per commit

Likely boundaries:

- signal evaluation / `TradeSignal` creation
- pure/pre-submit `EntryPolicy` and `EntryPlan`
- execution remains with TestnetExecutionAdapter
- TradingEngine becomes orchestration layer

Do not move all logic at once.

### C. Preserve exact effective ordering

```text
LIVE snapshot/gate/economics
-> sizing/depth economics
-> Demo best price
-> Demo IOC
-> confirmed fill
-> immediate position state
-> post-fill guard
```

## 13. Refactor anti-patterns already rejected

Do not reintroduce:

- wrapper chains
- launcher forks for every threshold experiment
- global mutable active state
- monkeypatching legacy engine globals
- deep environment loading
- discovery performing execution
- LIVE private auth as hidden Testnet dependency
- duplicate Profit Hold implementations
- software polling delays
- mandatory private position GET immediately after confirmed entry fill
- chase/top-up after partial IOC

## 14. Documentation/history added for Codex continuity

Current continuity files:

- `CODEX_HANDOFF_20260821.md` — full project/strategy/refactor handoff
- `CHAT_HISTORY.md` — direct ChatGPT project-history link and source index
- `DEVELOPER_HANDOFF_20260816.md` — older technical/research snapshot with historical measured results
- `AGENTS.md` — mandatory engineering constraints for agents
- `PROJECT_STATE.md` — this current canonical engineering state

Current ChatGPT project-history URL is stored in `CHAT_HISTORY.md`.

## 15. Testing and validation status

CI workflow: `.github/workflows/ci.yml`, Python 3.11, `pytest -q`.

The branch is not protected by mandatory status checks. Never infer success from a commit existing.

Before claiming an engineering change is complete:

```powershell
.\.venv\Scripts\python.exe -m pytest -q --basetemp .codex-test-tmp -p no:cacheprovider
```

or in an activated environment:

```text
pytest -q
```

After a material Testnet architecture change, the user runs the bot locally using the same existing `.env` and launcher to verify real Demo exchange behavior.

Do not claim the local/Testnet behavior is proven until that actual run occurs.

## 16. Current Definition of Done

The refactor succeeds only if:

- local startup remains the same
- existing `.env` remains valid
- discovery and pair-selection UX remains the same
- strict strategy/risk/exit semantics remain the same
- Testnet execution/network ordering remains behaviorally the same
- regression tests pass
- local Testnet run shows no behavior regression
- TradingEngine becomes orchestration rather than a monolith
- discovery, signal, entry, execution, position management, exits, risk and reporting have explicit boundaries

The goal is the same bot in cleaner blocks, not a cleaner-looking different bot.
