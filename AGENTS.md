# Agent instructions — canonical latency-arb

Before changing code, read `CANONICAL.md` and `PROJECT_STATE.md`.

The only active product branch is `canonical-latency-arb-v1`. Do not create another branch for ordinary fixes.

Do not rebuild the strategy from scratch. Do not change `BASELINE_V1` in place. Frozen alpha reference: `8a0bc6043385dbaf95ec8e77b93d91fd00a7f9e5`; known-good 100-trade paper source: `372c3b286eb82aa4b87d806999f8db47173a2b3e`.

Do not reintroduce archived semantics: sub-1bps microspread research, staged 3/5bps trailing, `TrailingStopTracker(5,5)`, proxy symbols, Testnet follower alpha, TESTNET_EXECUTION_ONLY PnL, fixed latency constants, or REST market-price lookups on the signal-critical path.

Preserve frozen entry/exit ordering exactly. Infrastructure work may improve latency measurement, state atomicity, metadata/risk preflight, IOC precision, reconciliation and telemetry, but must not silently alter alpha thresholds or exit priority.

Canonical modules:
- `canonical_bootstrap.py`: current read-only persistent-lag profile bootstrap;
- `canonical_shadow.py`: LIVE Binance -> LIVE MEXC read-only E2E alpha validation;
- `canonical_latency.py`: current transport latency proxy with spike/stall protection;
- `canonical_execution.py`: Testnet-only execution state machine;
- `canonical_testnet_calibrator.py`: real Testnet IOC/open/close timing calibration.

Latency must be measured continuously. A rolling percentile may not hide a current latest spike or in-flight stall. New entries require fresh latency. After an exit decision, Binance freshness must never delay the close.

Execution invariants: one IOC, partial fill accepted, no top-up/chase; private side-specific risk limits; metadata/leverage preflight before critical path; locks only around state transitions; no pre-close REST position lookup when an exact known position exists; reconcile after submit; never swallow close/reconciliation errors.

Testnet is an execution calibrator only. It does not define LIVE alpha or validate LIVE strategy PnL. Public Testnet writes require local `MEXC_DEMO_WRITE=YES`.

Before considering a code change complete, run the canonical CI test set locally or verify GitHub Actions. Never claim tests passed without evidence.
