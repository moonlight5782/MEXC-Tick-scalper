# PROJECT STATE — canonical

Current active product branch: `canonical-latency-arb-v1`.

The product reconstructs a Binance -> MEXC futures latency/lead-lag scalper. The alpha source is the frozen persistent-lag strategy already validated in paper form; older microspread, staged-trailing and Testnet-follower variants are not active strategy definitions.

Immutable references:
- `372c3b286eb82aa4b87d806999f8db47173a2b3e` — known-good 100-trade paper source.
- `8a0bc6043385dbaf95ec8e77b93d91fd00a7f9e5` — frozen baseline/reference.
- `src/mexc_tick_scalper/baseline_v1.py` — immutable parameter set.

## Implemented

- one canonical branch/source of truth;
- clean-clone profile bootstrap from current read-only LIVE Binance/MEXC data;
- canonical LIVE read-only E2E shadow with realtime latency, arrival recheck, sticky exit, actual timing telemetry and drain semantics;
- canonical `.env` loading and explicit Testnet write gate;
- canonical Testnet execution state machine with private side-specific risk limits, leverage/metadata preflight, single IOC/no chase, remote reconciliation and close-by-known-position;
- canonical Testnet calibrator measuring actual open/close execution timings, fills and fees;
- CI narrowed to canonical plus frozen dependencies;
- obsolete root launchers/artifacts and old `demo_*` subsystem removed from the canonical working tree.

## Current validation sequence

1. Install clean environment: `pip install -e . pytest`.
2. Run `start_canonical_shadow.bat`. It bootstraps a current persistent profile automatically if needed, then validates LIVE alpha read-only.
3. Run `start_canonical_testnet_calibrator.bat` only with local Testnet token and explicit `MEXC_DEMO_WRITE=YES`; this validates execution mechanics only.
4. Use actual execution timings to build/update realtime order-latency estimation before coupling execution to strategy. No fixed latency constants or fixed RTT multiplier are allowed.

## Not yet claimed

- CI pass has not been observed for the current canonical HEAD through the available connector.
- A fresh canonical shadow run has not yet been observed after the cleanup/bootstrap changes.
- A canonical Testnet calibrator run has not yet been observed.
- Real-money LIVE execution is not enabled or validated.

Detailed architecture and invariants: `CANONICAL.md`.
