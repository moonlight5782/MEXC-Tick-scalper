# MEXC-Tick-scalper — project state / Codex handoff

Last handoff update: 2026-08-14
Repository: `moonlight5782/MEXC-Tick-scalper`

This document is the continuity snapshot for another coding agent. Continue from the existing implementation. Do not restart the architecture from zero.

## 1. What this project is

The project is a reconstruction and improvement of an old very-fast MEXC futures scalper. The historical bot appears to have targeted tiny short-duration price inefficiencies with high leverage and zero trading fees.

The current strongest hypothesis is NOT ordinary local momentum trading. It is cross-exchange latency / price-lag convergence:

- Binance USD-M futures acts as the more liquid/faster leader.
- MEXC futures sometimes lags for milliseconds/seconds.
- A short-lived difference between Binance and MEXC appears.
- The bot enters on MEXC in the direction in which MEXC is expected to catch up.
- It exits when the cross-exchange residual converges, reverses, times out, becomes adverse, or a positive trailing stop is hit.

This is directional lead-lag/statistical latency trading when only the MEXC leg is opened. It is not risk-free two-leg arbitrage.

## 2. Historical reconstruction findings

Historical order exports analyzed earlier strongly suggested this execution pattern:

1. place a relatively large IOC LIMIT entry
2. accept partial fill
3. unfilled remainder is cancelled
4. do not market-top-up the entry
5. manage only the actually filled quantity
6. close with reduce-only market execution

Approximate historical observations from earlier exports:

### UNIUSDT
- ~685 IOC entries / ~684 exits
- leverage around 200x
- target notional around 20k USDT
- median fill roughly 7.65%
- profit factor roughly 1.45
- net around +121 USDT in the analyzed export
- winners tended to hold around 9.5-10s
- losers tended to hold around 2-3s

### BCHUSDT
- ~456 IOC entries / ~457 exits
- leverage around 195x
- target notional around 10k USDT
- median fill roughly 42.6%
- profit factor roughly 1.89
- net around +108 USDT in the analyzed export
- winners tended to hold around 10s
- losers around 5s

The exact old signal is still unknown.

A separate clue from the old bot suggested limit levels roughly 0.04%-0.10% (4-10 bps) away on some instrument and/or reacting to squeeze/wick moves. This may represent another version/mode, not necessarily the final mechanism.

## 3. Why the strategy direction changed

An earlier MEXC-only hybrid strategy used local trades/CVD/momentum/L2. Demo logs showed that this could enter when the actual forecasted move was far smaller than the executable Demo spread, and even sometimes with contradictory momentum.

Example failure mode observed:
- Demo spread around 6-8 bps
- local momentum often only ~0.1-0.5 bps
- entries lost approximately one spread almost immediately

This suggested that local MEXC flow was probably confirmation/veto information, not the real source of edge.

We then measured LIVE Binance -> LIVE MEXC differences across real zero-fee symbols. Real lead-lag events were clearly observable. Some pairs had useful raw edge but huge MEXC spread, while others had much better edge-after-spread economics.

Examples from a 60s LIVE scan at one point:
- `VELVET_USDT`: avg edge ~13.88 bps, LIVE spread ~1.07 bps, 11 events
- `BTW_USDT`: avg edge ~9.23 bps, LIVE spread ~4.35 bps, 12 events
- `BEAT_USDT`: avg edge ~15.62 bps but LIVE spread ~15.05 bps, so almost no executable advantage

This demonstrated that raw cross-exchange gap is not enough; executable LIVE spread matters.

## 4. LIVE zero-fee universe

Using the real MEXC web-account fee table in read-only mode, 91 symbols were discovered that simultaneously had:

- MEXC LIVE contract
- exact account `maker=0`
- exact account `taker=0`
- matching Binance USD-M perpetual

Examples included BCH, DOGE, SOL, XRP, LINK, SUI, UNI and many others.

Important distinction:

- LIVE account zero-fee universe is large.
- MEXC Demo/Testnet supports far fewer exact symbols.

At one recent run, the exact intersection of:

`LIVE MEXC 0/0 ∩ Binance USD-M ∩ MEXC Demo`

was 11 symbols:

- `ARB_USDT`
- `BCH_USDT`
- `DOGE_USDT`
- `LINK_USDT`
- `RAVE_USDT`
- `SEI_USDT`
- `SOL_USDT`
- `SUI_USDT`
- `WLFI_USDT`
- `XPL_USDT`
- `XRP_USDT`

This intersection is dynamic and should always be rediscovered, not hardcoded.

## 5. Hard safety architecture

The project currently separates LIVE signal data from Demo writes.

### Allowed LIVE use
- Binance public market data
- MEXC public market data
- MEXC account fee table and other necessary private read-only account data through `MEXC_WEB_TOKEN`

### Forbidden LIVE use
- no LIVE order placement
- no LIVE position-changing writes

The LIVE web adapter must be constructed with `write_enabled=False`.

### Allowed writes
Only MEXC Demo/Testnet using:
- `MEXC_DEMO_WEB_TOKEN`
- `MEXC_DEMO_WRITE=YES`

`MexcWebExecutionAdapter` validates Testnet/Demo host safety. Preserve these boundaries.

Do not expose or commit either token.

## 6. Fee logic

The user's core production requirement is exact zero trading commission.

For strategy eligibility:
- maker must be exactly 0
- taker must be exactly 0
- missing fee data is NOT zero
- stale fee state must block new entries

The LIVE fee table determines whether the real strategy would be tradable.

Demo/Testnet fee behavior is simulator-specific and must not redefine the real strategy universe.

## 7. Current active strategy: microspread crossing

The most recent user insight was: microspreads should appear constantly, so waiting only for large 4+ bps lead-lag impulses is likely the wrong reconstruction.

A prior continuous runner produced many tens of thousands of Binance quotes while `raw_ready=0`, because its readiness condition effectively required:
- large residual >= ~4 bps
- Binance move >= ~1 bps over ~250 ms
- additional leader/lagger direction conditions

That was too restrictive for micro-latency trading.

The current implementation therefore adds a dedicated microspread model and runner.

### Core idea

Continuously calculate:

`raw_gap_bps = 10000 * log(Binance_mid / MEXC_mid)`

Maintain a robust short rolling baseline of the normal cross-exchange basis.

Then:

`residual_edge_bps = raw_gap_bps - baseline_gap_bps`

Trade short excursions around the normal basis rather than only rare large absolute gaps.

### Important baseline behavior

The newest portion of the data is excluded from baseline estimation so a temporary lag cannot immediately teach itself into the normal basis.

Current defaults include approximately:
- baseline window: 8s
- baseline exclusion: 1000ms
- micro horizon: 100ms

Median/robust baseline is used rather than treating the newest excursion as normal.

### Current micro thresholds

Current starting Demo/research defaults are approximately:
- minimum residual edge: 0.35 bps
- minimum net edge after LIVE spread: 0.20 bps
- edge/spread ratio: 1.05
- minimum Binance micro move: 0.02 bps
- Binance freshness ~300ms
- MEXC depth freshness tolerance ~2000ms
- entry cooldown ~250ms
- min hold ~50ms
- max hold ~15s

These are research defaults, NOT proven optimal parameters. Calibrate from measured Demo/shadow results.

### Why MEXC freshness is looser than Binance

If Binance updates but MEXC book remains unchanged, that lack of update may itself represent the lag we are trying to exploit. Therefore the strategy must not label MEXC as stale after only ~250ms merely because its quote did not change.

Binance should remain tightly fresh; MEXC book can be older while the WebSocket feed remains healthy.

## 8. Hysteresis / rearm

One persistent microspread must not create multiple entries.

Current design:
- model is armed initially
- crossing threshold emits one opportunity
- model becomes disarmed
- it rearms only after residual converges close enough to normal basis, or crosses through the opposite side

This is essential for interpreting repeated websocket updates as one excursion rather than dozens of independent trades.

## 9. Entry economics

Entry should be based on LIVE MEXC executable spread, not last trade and not Demo spread.

Current threshold concept:

`required_edge = max(min_edge, LIVE_spread + min_net_edge, LIVE_spread * spread_ratio)`

Therefore a 0.7 bps residual is only useful if the LIVE MEXC bid/ask spread leaves positive remaining edge.

Do not optimize for raw residual alone.

## 10. Event-driven market path

The latest microspread path is intended to be event-driven:

- Binance USD-M `bookTicker` updates local leader state.
- MEXC LIVE depth updates local executable bid/ask state.
- either market update wakes the strategy immediately.
- the strategy recalculates residual without waiting for a fixed 50ms polling cycle.

Avoid adding REST market-data calls to the entry-critical path.

REST/non-critical calls are acceptable for:
- symbol discovery
- contract metadata
- fee refresh
- Demo-specific execution/position reconciliation

## 11. Demo execution semantics

Signals and modeled economics are LIVE.
Actual orders are TESTNET/Demo only.

The intended sequence is:

`LIVE Binance + LIVE MEXC -> microspread candidate -> LIVE fee check -> Demo price/IOC -> Demo position reconciliation -> manage using LIVE economics -> flatten Demo`

The Demo order book may be unrealistic and wider than LIVE. Do not use Demo spread as the strategy's economic edge requirement.

However, Demo order mechanics are still useful for validating:
- IOC behavior
- partial fills
- remote position visibility
- reduce-only closes
- timeout/retry behavior
- cleanup/flatten safety

## 12. Known Testnet race condition

MEXC Testnet has shown a behavior where an IOC request reports a positive fill but `get_position()` does not show the position immediately.

Earlier this caused a fatal error:
`IOC fill reported but position did not appear after reconciliation`

The adapter contains bounded visibility waiting and the microspread runner now
adds a persistent pending-entry state for confirmed IOC fills. While the remote
position remains invisible, the locally confirmed fill is managed provisionally,
all new entries stay blocked, and reconciliation continues past the old five-second
limit and past normal session/trade-count limits. Once visible, the exact remote
position replaces the provisional snapshot and any already-triggered exit is sent
reduce-only.

Do not remove this protection casually. A manually interrupted process still
depends on startup/shutdown Demo flattening, so Demo must be confirmed flat before
and after every experiment.

## 13. Exit logic

Current intended exit causes include:
- residual convergence toward normal Binance/MEXC basis
- residual reversal
- adverse executable LIVE move
- maximum hold timeout
- positive trailing stop

For microspread trading, convergence is expected to be the natural primary exit when MEXC catches Binance.

## 14. Positive trailing requirement

The user explicitly wants floating stop protection in PROFIT, not intentionally letting a winner ratchet back into a loss.

Existing staged approach:
- MFE >= +3 bps -> floor roughly +0.5 bps
- MFE >= +5 bps -> floor roughly +2.0 bps
- mature winner -> trailing follows peak at a configured distance
- stop only ratchets tighter; never loosens

Earlier implementation lived in `AsymmetricExitPolicy`; newer research runners also use `PositiveTrailing`.

Caveat: a software stop cannot guarantee a positive realized fill due to slippage/gaps/API latency. Preserve accurate wording in logs/docs.

## 15. Risk controls

Current/desired controls:
- one simultaneous position maximum by default
- maximum session loss / drawdown halt
- adverse cut
- maximum hold timeout
- startup flatten
- shutdown flatten
- fee freshness gate
- no new trade if fee becomes nonzero/unknown

Do not compensate for weak signal quality by increasing leverage.

## 16. Current important files

### Current microspread path
- `src/mexc_tick_scalper/microspread.py`
  - robust cross-exchange basis
  - residual calculation
  - sub-1-bps readiness
  - hysteresis/rearm
- `src/mexc_tick_scalper/microspread_feed.py`
  - event-driven Binance/MEXC LIVE feeds
- `src/mexc_tick_scalper/demo_microspread_test.py`
  - LIVE signal + LIVE economics + Demo/Testnet execution
- `src/mexc_tick_scalper/demo_live_launcher.py`
  - launched by `start_demo.bat`; now intended to launch `demo_microspread_test`
- `tests/test_microspread.py`
  - tests sub-1-bps crossings, hysteresis, quote-age behavior, etc.

### Older lead-lag path kept for comparison
- `src/mexc_tick_scalper/lead_lag.py`
- `src/mexc_tick_scalper/demo_multi_lead_lag_test.py`
- `src/mexc_tick_scalper/live_lead_lag_scan.py`
- `src/mexc_tick_scalper/live_lead_lag_shadow.py`

Do not delete these yet; they are useful for comparison/research.

### Execution and safety
- `src/mexc_tick_scalper/web_execution.py`
- `src/mexc_tick_scalper/demo_hybrid_test.py`
- `src/mexc_tick_scalper/demo_position_manager.py`
- `src/mexc_tick_scalper/web_fee.py`
- `src/mexc_tick_scalper/live_zero_fee_universe.py`

### Earlier local-MEXC strategy
- `src/mexc_tick_scalper/hybrid_strategy.py`
- `src/mexc_tick_scalper/orderbook_signal.py`

These are no longer assumed to contain the primary predictive signal, but pieces of confirmation/risk logic may remain useful.

## 17. Current launcher/user workflow

User works on Windows PowerShell, repository typically located at:

`D:\Mexc_tick_scalper\MEXC-Tick-scalper`

Typical update/test/run:

```powershell
cd D:\Mexc_tick_scalper\MEXC-Tick-scalper
git pull --ff-only origin main
python -m pytest -q
.\start_demo.bat
```

The user prefers direct autonomous engineering work and does not want fragile generated updater scripts. Make normal repository commits instead.

## 18. Testing / CI

CI workflow:
- `.github/workflows/ci.yml`
- Python 3.11
- `pip install -e . pytest`
- `pytest -q`

The microspread branch passed CI before merge. The code baseline that introduced the current microspread mode was commit:

`e3eb18fe0407a96e98874f8ff92842b3d40d1e85`

`AGENTS.md` and this handoff were added afterward for Codex continuity.

Always re-run tests after changes. Never claim green tests without actual output.

## 19. Current evidence and unresolved questions

### Evidence supporting lead-lag/microspread
- LIVE Binance/MEXC scanner observed many real cross-exchange divergences.
- Different pairs show radically different edge-after-spread quality.
- User expectation that microspreads occur frequently is consistent with the need to inspect sub-1-bps residuals instead of waiting only for large events.
- Historical winner/loser holding durations are compatible with convergence-style behavior, but do not prove it.

### Still unresolved
- exact old signal formula
- exact old threshold(s)
- whether old bot used resting maker orders in some versions
- whether Binance was definitely the leader or just one possible leader
- exact latency distribution Binance -> MEXC by symbol/time
- whether exchange timestamps vs local receive timestamps materially change measured edge
- which residual size remains profitable after real executable spread/slippage
- ideal exit convergence level
- ideal positive trailing activation for sub-1-bps strategies

Do not pretend these are solved. Instrument and measure them.

## 20. Highest-value next work for Codex

Prioritize measurement before parameter guessing.

### A. Run and inspect the new microspread Demo mode
Expected heartbeat should expose something like:
- number of symbols above residual floor
- maximum current residual
- candidate count after LIVE spread economics
- Binance quote count
- MEXC depth count

Goal: confirm microspread residuals are actually being seen frequently.

### B. Add microspread telemetry if missing
For every excursion, ideally log/CSV:
- symbol
- timestamps/local receive times
- Binance bid/ask/mid
- MEXC LIVE bid/ask/mid
- raw gap
- baseline gap
- residual
- MEXC spread
- required edge
- direction
- B move over 25/50/100/250ms if practical
- MEXC move over same horizons
- whether candidate would enter
- rejection reason
- future MEXC executable returns at +25/+50/+100/+250/+500/+1000ms

This is probably the most valuable next research artifact.

### C. Measure causality, not only correlation
For each potential signal, test whether Binance move systematically precedes MEXC executable move. Compare against matched/random controls.

Useful lag buckets:
- 25ms
- 50ms
- 100ms
- 250ms
- 500ms
- 1000ms

Use local receive timestamps and exchange timestamps separately where available.

### D. Calibrate economic threshold
Find by symbol:
- residual distribution
- MEXC LIVE spread distribution
- future executable return conditional on residual
- hit rate
- average favorable/adverse excursion
- profit factor in zero-fee shadow model

Then replace arbitrary defaults with data-driven per-symbol thresholds.

### E. Latency telemetry
Measure:
- signal detected -> Demo price request start
- price response
- IOC POST start
- IOC response
- position visible
- close request/response

Do not claim sub-100ms execution until measured.

### F. Keep Demo and shadow research separate
Because Testnet prices/spreads may be unrealistic, evaluate strategy economics using LIVE bid/ask shadow PnL while using Demo only to validate order mechanics.

## 21. What NOT to do next

- Do not switch PRIVATE execution to LIVE.
- Do not remove fee=0 requirement.
- Do not optimize only for more trades.
- Do not hardcode the 11-symbol Demo intersection.
- Do not restore a universal 4-bps entry threshold without evidence.
- Do not use last trade as executable entry/exit price when bid/ask is available.
- Do not increase leverage to make weak edge look profitable.
- Do not rewrite the whole project or discard existing execution/reconciliation code.
- Do not claim the strategy is profitable before sufficient shadow/Demo evidence.

## 22. First prompt to give Codex

Use this after opening the repository in Codex:

> Read AGENTS.md and PROJECT_STATE.md completely. Inspect the current main branch and the current microspread runner/tests before editing anything. Continue the existing reconstruction; do not rebuild it. First verify that `start_demo.bat` now launches the event-driven microspread mode and that the heartbeat exposes enough telemetry to prove whether sub-1-bps residuals are actually occurring. Then run the tests. If telemetry is insufficient, add structured per-excursion logging/CSV and tests while preserving the LIVE-read-only / Demo-write-only safety boundary. Commit the changes and report measured results, remaining uncertainty, and the exact next experiment.

That prompt should be enough for a fresh Codex session to pick up the project without this entire ChatGPT conversation.

## 23. Frozen successful zero-fee-gross candidate

The 2026-08-15 Binance-only impulse experiment is preserved as the named runner
profile `binance-impulse-zero-fee-gross-v1`.

It is classified as a **successful Demo gross candidate under the zero-fee
counterfactual**, not as proven LIVE profitability. The frozen strategy uses:

- Binance-only 100ms impulse entry; MEXC LIVE is not an entry-signal input
- `XRP_USDT`, `LINK_USDT`, and `DOGE_USDT`
- LIVE-account exact maker=0/taker=0 eligibility gate, read-only
- MEXC Demo/Testnet IOC writes only, with Demo fees measured separately
- 10,000 USDT requested IOC notional, isolated margin, up to 200x leverage
- 1.0 bps minimum Binance impulse plus Demo executable-spread economics
- asynchronous provisional position reconciliation
- protected exits, 0.5 bps minimum exit profit, 60s maximum hold
- fixed -50 USDT gross session-loss halt

Reproduce the strategy parameters while choosing fresh session/telemetry limits:

```powershell
.\.venv\Scripts\python.exe -m mexc_tick_scalper.demo_microspread_test `
  --strategy-profile binance-impulse-zero-fee-gross-v1 `
  --session-seconds 21600 --max-cycles 100 `
  --excursion-csv <new-excursion.csv> --residual-csv <new-residual.csv>
```

Measured result from the original run:

- 91 normally logged exits plus one separately reconciled emergency exit
- zero-fee Demo gross: +232.3418 USDT
- Demo fees: 367.9385 USDT
- actual Demo net: -135.5967 USDT
- gross wins/losses/flats: 68/20/4
- non-flat gross win rate: 77.27%
- approximate gross profit factor: 4.89
- median/p95 hold: 5.37s / 60.00s
- median/p95 IOC confirmation: 642.9ms / 1766.6ms
- median/p95 position visibility: 1041.6ms / 5049.5ms

The original run stopped before 100 normal exits because a confirmed XRP IOC
remained temporarily invisible beyond the old bounded reconciliation wait.
Persistent pending-entry reconciliation has since replaced that failure path.
Before treating this profile as a product candidate, complete an independent
100-trade validation against executable LIVE bid/ask shadow prices using the
measured latency distribution. LIVE writes remain forbidden.

## 24. Read-only LIVE Binance-impulse shadow

`src/mexc_tick_scalper/live_binance_impulse_shadow.py` reproduces the frozen
Binance-impulse candidate against current executable MEXC LIVE bid/ask without
constructing an order-capable adapter. It uses public Binance/MEXC WebSockets
and the MEXC private fee table only through the existing read-only discovery
path.

Default stress assumptions are deliberately less favorable than signal-time
paper fills:

- one virtual position globally
- 10,000 USDT virtual notional
- 650ms signal-to-entry fill delay
- 350ms exit-decision-to-fill delay
- 0.5 bps additional slippage on each side
- actual MEXC ask/bid at the delayed fill timestamp
- refreshed exact LIVE maker=0/taker=0 eligibility; unknown/nonzero blocks entry

This shadow validates market economics, not exchange fill quantity. The current
depth feed retains top-of-book prices but not enough depth levels to prove that
the full 10,000 USDT IOC would fill. Treat its PnL as a latency/slippage stress
estimate and add depth-aware partial-fill simulation if the 100-trade result is
otherwise promising.
