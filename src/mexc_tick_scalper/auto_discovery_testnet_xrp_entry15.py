from __future__ import annotations

# Thin launcher policy override only: restore the exact XRP Testnet entry gate that
# was producing trades before profit-hold work. All latency and winner-management
# behavior remains in auto_discovery_testnet_xrp_fast unchanged.
from . import auto_discovery_testnet_xrp_fast as fast


def main() -> None:
    fast.TESTNET_MIN_RESIDUAL_BPS = 15.0
    fast.TESTNET_MIN_STRENGTH_RATIO = 4.0
    fast.main()


if __name__ == "__main__":
    main()
