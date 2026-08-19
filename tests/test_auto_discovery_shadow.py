from types import SimpleNamespace

import mexc_tick_scalper.auto_discovery_shadow as auto


def test_survival_recomputed_at_current_latency():
    lifetimes = [100.0, 250.0, 400.0, 800.0]
    assert auto._survival_at(lifetimes, 300.0) == 0.5
    assert auto._survival_at(lifetimes, 900.0) == 0.0


def test_effective_leverage_is_capped_per_live_contract():
    old = dict(auto.CONTRACTS)
    try:
        auto.CONTRACTS.clear()
        auto.CONTRACTS["AAA_USDT"] = SimpleNamespace(max_leverage=20)
        auto.CONTRACTS["BBB_USDT"] = SimpleNamespace(max_leverage=500)
        assert auto._effective_leverage("AAA_USDT") == 20.0
        assert auto._effective_leverage("BBB_USDT") == 200.0
    finally:
        auto.CONTRACTS.clear()
        auto.CONTRACTS.update(old)


def test_score_rewards_current_survival():
    profile = SimpleNamespace(
        median_signal_residual_bps=30.0,
        median_signal_strength_ratio=4.0,
        signals=20,
        convergence_rate=0.7,
        reversal_rate=0.1,
    )
    assert auto._score(profile, 0.8) > auto._score(profile, 0.4)
