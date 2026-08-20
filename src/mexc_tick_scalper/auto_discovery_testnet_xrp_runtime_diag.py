from __future__ import annotations

import time

from . import auto_discovery_testnet_xrp_fixed as fixed
from . import auto_discovery_testnet_xrp_profit_hold as profit_hold


_ORIGINAL_GATE = fixed.LeadLagGate


class DiagnosticLeadLagGate(_ORIGINAL_GATE):
    """Pure-observation wrapper: never changes the gate decision."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._last_diag_ms = 0

    def observe(self, symbol, snap, spread_bps, now_ms, *, event_key=None):
        decision = super().observe(
            symbol,
            snap,
            spread_bps,
            now_ms,
            event_key=event_key,
        )
        if now_ms - self._last_diag_ms >= 1000:
            self._last_diag_ms = now_ms
            strength = abs(decision.residual_bps) / max(decision.threshold_bps, 1e-12)
            absolute_ok = abs(decision.residual_bps) >= 8.0
            strength_ok = strength >= 3.0
            fixed.console.print(
                "ENTRY DIAG "
                f"{symbol} ready={decision.ready} reason={decision.reason} "
                f"residual={decision.residual_bps:+.2f}bps "
                f"threshold={decision.threshold_bps:.2f}bps strength={strength:.2f}x "
                f"spread={float(spread_bps):.2f}bps "
                f"binance_move={decision.binance_move_bps:+.2f}bps "
                f"mexc_move={decision.mexc_move_bps:+.2f}bps "
                f"leader_adv={decision.leader_advantage_bps:+.2f}bps "
                f"absolute8={'OK' if absolute_ok else 'NO'} "
                f"strength3={'OK' if strength_ok else 'NO'}"
            )
        return decision


def main() -> None:
    original = fixed.LeadLagGate
    fixed.LeadLagGate = DiagnosticLeadLagGate
    try:
        profit_hold.main()
    finally:
        fixed.LeadLagGate = original


if __name__ == "__main__":
    main()
