from types import SimpleNamespace

import mexc_tick_scalper.demo_lead_lag_test as lead_demo


def test_lead_lag_runner_imports_and_zero_fee_gate_is_exact():
    assert lead_demo._zero_fee_confirmed(SimpleNamespace(maker=0.0, taker=0.0))
    assert not lead_demo._zero_fee_confirmed(SimpleNamespace(maker=0.0, taker=0.0001))
    assert not lead_demo._zero_fee_confirmed(SimpleNamespace(maker=0.0001, taker=0.0))
