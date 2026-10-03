# tests/test_pacing.py
from avito_ams.pacing import recommend_bid_adjustment, target_spend_by_hour


def test_target_spend_at_midnight_zero():
    target = target_spend_by_hour(
        daily_budget=2400, hour=0, peak_hours=[19, 20, 21, 22], peak_pct=30
    )
    assert target == 0


def test_target_spend_uniform_outside_peak():
    # 2400 / 24 = 100 per hour, no peak adjustment until hour 19+
    target = target_spend_by_hour(
        daily_budget=2400, hour=12, peak_hours=[19, 20, 21, 22], peak_pct=30
    )
    # By hour 12, expected uniform = 12 * 100 = 1200 (peak hours not reached yet)
    assert target == 1200


def test_target_spend_includes_peak_boost():
    # By hour 22 (end of peak block 19-22), peak budget = 30% of 2400 = 720
    # Off-peak hours (0-18, 23): 20 hours, share = 70% of 2400 = 1680
    # Per off-peak hour: 1680/20 = 84
    # Peak hours (19-22): 4 hours, share = 720, per hour = 180
    # Cumulative at hour 22 = 19*84 + 4*180 = 1596 + 720 = 2316
    target = target_spend_by_hour(
        daily_budget=2400, hour=22, peak_hours=[19, 20, 21, 22], peak_pct=30
    )
    assert target == 2316


def test_full_day_equals_budget():
    target = target_spend_by_hour(
        daily_budget=2400, hour=23, peak_hours=[19, 20, 21, 22], peak_pct=30
    )
    # By hour 23 (end of day): 20*84 + 4*180 = 1680 + 720 = 2400
    assert target == 2400


def test_under_pace_recommends_increase():
    # spent only 50% of expected → bump bid up
    rec = recommend_bid_adjustment(actual_spent=400, target_spent=1000, current_bid=10)
    assert rec.action == "increase"
    assert rec.new_bid > 10


def test_over_pace_recommends_decrease():
    rec = recommend_bid_adjustment(actual_spent=1500, target_spent=1000, current_bid=10)
    assert rec.action == "decrease"
    assert rec.new_bid < 10


def test_within_threshold_no_change():
    rec = recommend_bid_adjustment(actual_spent=950, target_spent=1000, current_bid=10)
    assert rec.action == "hold"
    assert rec.new_bid == 10
