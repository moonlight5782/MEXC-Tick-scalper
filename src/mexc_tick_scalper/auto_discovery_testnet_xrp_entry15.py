from __future__ import annotations

# Compatibility launcher only. Preserve the original XRP Testnet entry gate:
# residual >= 8 bps and strength >= 3x. All latency and winner-management
# behavior remains in auto_discovery_testnet_xrp_fast.
from . import auto_discovery_testnet_xrp_fast as fast


def main() -> None:
    fast.TESTNET_MIN_RESIDUAL_BPS = 8.0
    fast.TESTNET_MIN_STRENGTH_RATIO = 3.0
    fast.main()


if __name__ == "__main__":
    main()
