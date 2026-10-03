# tests/test_budget_limits.py
import pytest

from avito_ams.budget_limits import BudgetLimits


def _limits(**overrides):
    fields = dict(
        max_daily_per_item_rub=500,
        max_daily_total_rub=5000,
        stop_loss_cpa_rub=3000,
        target_cpa_rub=1000,
        auto_apply_limit_rub=200,
        pacing_check_interval_hours=2,
        peak_hours=[19, 20, 21, 22],
        peak_budget_pct=30,
    )
    fields.update(overrides)
    return BudgetLimits(**fields)


def test_valid_limits_use_default_grace_days():
    assert _limits().stop_loss_grace_days == 3


def test_negative_budget_rejected():
    with pytest.raises(Exception):
        _limits(max_daily_per_item_rub=-1)


def test_target_above_stop_loss_rejected():
    with pytest.raises(Exception, match="target_cpa_rub"):
        _limits(stop_loss_cpa_rub=1000, target_cpa_rub=2000)  # inverted!


def test_peak_hour_out_of_range_rejected():
    with pytest.raises(Exception):
        _limits(peak_hours=[24])
