# CANONICAL SOURCE OF TRUTH

Active product branch: `canonical-latency-arb-v1`. Do not create another product branch for ordinary fixes.

## Immutable alpha reference

- frozen commit: `8a0bc6043385dbaf95ec8e77b93d91fd00a7f9e5`
- known-good 100-trade alpha source: `372c3b286eb82aa4b87d806999f8db47173a2b3e`
- parameters live in `src/mexc_tick_scalper/baseline_v1.py` and must not be edited in place.

Frozen entry invariants include residual >= 8 bps, signal strength >= 3x, residual retention >= 60%, Binance impulse retention >= 75%, one partial IOC model, <=1 bps cross/slippage, and executable edge above round-trip cost by the frozen reserve/ratio.

Frozen exit order is:
1. mid_adverse_cut
2. leader_retrace
3. residual_reversal
4. mexc_catchup_convergence
5. no_progress
6. positive_trailing_stop
7. timeout

Do not replace this with microspread 0.35 bps, staged 3/5 bps trailing, `TrailingStopTracker(5,5)`, proxy symbols, Testnet follower alpha, or a different universe.

## Canonical LIVE alpha validation

`start_canonical_shadow.bat` is the only alpha-validation launcher.

Leader = LIVE Binance USD-M.
Follower = LIVE MEXC.
Universe = current persistent-lag profile intersect current LIVE exact maker=0/taker=0 and Binance cross-listing.

A clean clone with no local lifetime CSV first runs `mexc_tick_scalper.canonical_bootstrap`. The bootstrap collects current read-only LIVE lifetime data using every shared BASELINE_V1 setting. If the current sample yields no eligible persistent pair, it fails instead of weakening thresholds.

The shadow is read-only. Entry and exit are both evaluated on LIVE MEXC depth at their modeled arrival times. Realtime latency has no fixed entry/exit constants. The current proxy cannot hide a latest spike or in-flight request stall behind rolling p75. New entries require a fresh measurement and entry is fully revalidated at arrival.

After an EXIT DECISION, Binance state can no longer delay execution. Only elapsed latency and fresh MEXC depth are relevant to the simulated close. Session/max-signal limits stop NEW signals only; an already-open position must drain to terminal close. CSV records scheduled and actual entry/exit arrival timing separately, including schedule overrun.

The LIVE private-read latency is explicitly a transport proxy, not a claim of IOC matching-engine latency.

## Canonical Testnet execution

`src/mexc_tick_scalper/canonical_execution.py` is the only active execution-controller implementation.

Execution state machine:
`FLAT -> ENTRY_PENDING -> OPEN -> EXIT_PENDING -> RECONCILING -> FLAT`.

Rules:
- MEXC Testnet host boundary only; no LIVE writes;
- explicit `MEXC_DEMO_WRITE=YES` required by the public Testnet launcher;
- private `/private/account/risk_limit` capacity per symbol and LONG/SHORT side;
- leverage setup and contract metadata prewarm before the critical trading path;
- locks protect state transitions only, never network waits;
- one IOC only, partial fill accepted, no top-up/chase;
- actual `dealVol`, `dealAvgPrice`, fees and remote position are execution truth;
- 8819 disables that symbol/side for the run; it does not retry the same stale signal;
- close uses the already-known exact `PositionSnapshot/positionId`; no blocking `get_position()` before close submit;
- reconciliation occurs after submit and ambiguous outcomes are resolved from remote state.

`start_canonical_testnet_calibrator.bat` uses this engine to measure real Testnet execution timings. It is an execution calibrator only: Testnet price/PnL must never redefine or validate LIVE Binance->LIVE MEXC alpha.

## Latency product rule

A future strategy+execution coupling must use continuously updated current measurements. Do not introduce fixed 650/350/166 ms values or a fixed GET-RTT multiplier. Real order timings returned by canonical execution must feed a rolling execution-latency estimator, while the LIVE private probe continues to detect current transport spikes/stalls.

## Repository policy

Historical `demo_*` subsystem and obsolete launchers/tests are removed from the canonical working tree. Old branches remain Git history/research only. Frozen helper modules still imported by canonical are retained until their logic is moved without semantic changes.

`main`, `feature/mexc-binance-live`, `testnet-known-good-v1`, `testnet-frozen-latency-v1`, `latency-arb-product-v1`, `staged-trailing-stop`, and `agent/*` are not product source-of-truth branches.

No real-money LIVE execution is considered validated or enabled by this branch.
