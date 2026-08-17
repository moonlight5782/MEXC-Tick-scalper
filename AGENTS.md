# Agent instructions

Read `PROJECT_HANDOFF.md` first. It is the canonical source of truth.

## Non-negotiable rules

- Immutable known-good reference: branch `known-good-100trade-372c3b2`, commit `372c3b286eb82aa4b87d806999f8db47173a2b3e`.
- Active development branch: `testnet-known-good-v1`.
- Do not modify the known-good branch.
- Do not change alpha thresholds or exit semantics to fix Testnet/exchange problems.
- Preserve single-attempt partial IOC behavior: request target once, manage only actual Testnet fill, never top-up/chase the remainder.
- Testnet execution must use the same symbol as the signal. No proxy substitution.
- Testnet remote position/order state is the execution source of truth.
- Use only `MEXC_DEMO_WEB_TOKEN` for Testnet. Never expose secrets.
- Testnet writes must remain hard-bound to `futures.testnet.mexc.com`; do not send LIVE orders.
- Keep leverage/risk safeguards separate from the alpha strategy.
- Do not restore automatic exchange-maximum leverage.
- On entry/exit error, reconcile remote position before deciding what happened.
- On shutdown/error, attempt reduce-only flatten and verify no residual position remains.
- Add regression tests for execution/risk bugs.
- Never claim a real Testnet trade unless an actual Testnet fill/position is confirmed.

## Supported launchers

- `start_live_shadow.bat` — known-good LIVE-data paper reference.
- `start_testnet_known_good_v1.bat` — active real Testnet execution/risk validation.

Historical `demo_*`, scanners, probes and older strategy runners are not authoritative unless explicitly referenced by `PROJECT_HANDOFF.md` or imported by active code.
