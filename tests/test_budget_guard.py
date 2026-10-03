# tests/test_budget_guard.py
import pytest

from avito_ams.budget_guard import BudgetGuard
from avito_ams.budget_limits import BudgetLimits


@pytest.fixture
def limits():
    return BudgetLimits(
        max_daily_per_item_rub=500,
        max_daily_total_rub=5000,
        stop_loss_cpa_rub=3000,
        target_cpa_rub=1000,
        auto_apply_limit_rub=200,
        pacing_check_interval_hours=2,
        peak_hours=[19, 20, 21, 22],
        peak_budget_pct=30,
    )


def test_under_limits_auto_apply(limits):
    guard = BudgetGuard(limits, promo_balance_rub=10000)
    decision = guard.can_spend(
        item_id="1", amount_rub=100, item_spent_today=0, total_spent_today=0
    )
    assert decision.allowed is True
    assert decision.requires_confirm is False


def test_above_auto_apply_limit_requires_confirm(limits):
    guard = BudgetGuard(limits, promo_balance_rub=10000)
    decision = guard.can_spend(
        item_id="1", amount_rub=300, item_spent_today=0, total_spent_today=0
    )
    assert decision.allowed is True
    assert decision.requires_confirm is True


def test_exceeds_per_item_blocks(limits):
    guard = BudgetGuard(limits, promo_balance_rub=10000)
    decision = guard.can_spend(
        item_id="1", amount_rub=200, item_spent_today=400, total_spent_today=400
    )
    assert decision.allowed is False
    assert "MAX_DAILY_PER_ITEM" in decision.reason


def test_exceeds_total_blocks(limits):
    guard = BudgetGuard(limits, promo_balance_rub=10000)
    decision = guard.can_spend(
        item_id="2", amount_rub=200, item_spent_today=0, total_spent_today=4900
    )
    assert decision.allowed is False
    assert "MAX_DAILY_TOTAL" in decision.reason


def test_insufficient_balance_blocks(limits):
    # rule: balance must be >= 2x amount
    guard = BudgetGuard(limits, promo_balance_rub=300)
    decision = guard.can_spend(
        item_id="1", amount_rub=200, item_spent_today=0, total_spent_today=0
    )
    assert decision.allowed is False
    assert "BALANCE" in decision.reason


def test_stop_loss_blocks(limits):
    guard = BudgetGuard(limits, promo_balance_rub=10000)
    decision = guard.can_spend(
        item_id="1",
        amount_rub=100,
        item_spent_today=0,
        total_spent_today=0,
        item_cpa_rub=4000,
        item_high_cpa_days=4,
    )
    assert decision.allowed is False
    assert "STOP_LOSS" in decision.reason


def test_stop_loss_grace_period(limits):
    """If high CPA but only 1 day, still allow (give time to converge)."""
    guard = BudgetGuard(limits, promo_balance_rub=10000)
    decision = guard.can_spend(
        item_id="1",
        amount_rub=100,
        item_spent_today=0,
        total_spent_today=0,
        item_cpa_rub=4000,
        item_high_cpa_days=1,
    )
    assert decision.allowed is True


def test_zero_amount_rejected(limits):
    guard = BudgetGuard(limits, promo_balance_rub=10000)
    decision = guard.can_spend(
        item_id="1", amount_rub=0, item_spent_today=0, total_spent_today=0
    )
    assert decision.allowed is False


def test_stop_loss_grace_days_from_config():
    from avito_ams.budget_guard import BudgetGuard
    from avito_ams.budget_limits import BudgetLimits
    limits = BudgetLimits(
        max_daily_per_item_rub=1000, max_daily_total_rub=5000,
        stop_loss_cpa_rub=3000, target_cpa_rub=1500,
        auto_apply_limit_rub=200, pacing_check_interval_hours=2,
        peak_hours=[10, 11, 12], peak_budget_pct=40,
        stop_loss_grace_days=5,
    )
    guard = BudgetGuard(limits, promo_balance_rub=10000)
    # 4 days high CPA — under custom grace=5 → allowed
    d = guard.can_spend(item_id="i1", amount_rub=100, item_spent_today=0,
                        total_spent_today=0, item_cpa_rub=4000, item_high_cpa_days=4)
    assert d.allowed, d.reason
    # 6 days high CPA — over grace=5 → blocked with STOP_LOSS reason
    d = guard.can_spend(item_id="i1", amount_rub=100, item_spent_today=0,
                        total_spent_today=0, item_cpa_rub=4000, item_high_cpa_days=6)
    assert not d.allowed
    assert "STOP_LOSS" in d.reason


def test_concurrent_reservations_serialize(tmp_path):
    from avito_ams.budget_guard import BudgetGuard, _ReservationLedger
    from avito_ams.budget_limits import BudgetLimits

    limits = BudgetLimits(
        max_daily_per_item_rub=200, max_daily_total_rub=300,
        stop_loss_cpa_rub=5000, target_cpa_rub=1000,
        auto_apply_limit_rub=500, pacing_check_interval_hours=2,
        peak_hours=[10], peak_budget_pct=40, stop_loss_grace_days=3,
    )
    ledger = _ReservationLedger(tmp_path / "res.json")
    g1 = BudgetGuard(limits, promo_balance_rub=10000, ledger=ledger)
    g2 = BudgetGuard(limits, promo_balance_rub=10000, ledger=ledger)

    d1 = g1.reserve(item_id="i1", amount_rub=150, item_spent_today=0, total_spent_today=0)
    assert d1.allowed
    d2 = g2.reserve(item_id="i1", amount_rub=150, item_spent_today=0, total_spent_today=0)
    assert not d2.allowed

    g1.release(d1.reservation_id)

    d3 = g2.reserve(item_id="i1", amount_rub=150, item_spent_today=0, total_spent_today=0)
    assert d3.allowed


def test_reserve_returns_id_and_release_clears(tmp_path):
    from avito_ams.budget_guard import BudgetGuard, _ReservationLedger
    from avito_ams.budget_limits import BudgetLimits

    limits = BudgetLimits(
        max_daily_per_item_rub=1000, max_daily_total_rub=5000,
        stop_loss_cpa_rub=3000, target_cpa_rub=1500,
        auto_apply_limit_rub=200, pacing_check_interval_hours=2,
        peak_hours=[10], peak_budget_pct=40, stop_loss_grace_days=3,
    )
    ledger = _ReservationLedger(tmp_path / "res.json")
    g = BudgetGuard(limits, promo_balance_rub=10000, ledger=ledger)
    d = g.reserve(item_id="i1", amount_rub=100, item_spent_today=0, total_spent_today=0)
    assert d.allowed and d.reservation_id is not None
    assert ledger.reserved_total("i1") == 100
    g.release(d.reservation_id)
    assert ledger.reserved_total("i1") == 0


def test_reserve_without_ledger_falls_back_to_can_spend(tmp_path):
    from avito_ams.budget_guard import BudgetGuard
    from avito_ams.budget_limits import BudgetLimits

    limits = BudgetLimits(
        max_daily_per_item_rub=1000, max_daily_total_rub=5000,
        stop_loss_cpa_rub=3000, target_cpa_rub=1500,
        auto_apply_limit_rub=200, pacing_check_interval_hours=2,
        peak_hours=[10], peak_budget_pct=40, stop_loss_grace_days=3,
    )
    g = BudgetGuard(limits, promo_balance_rub=10000)  # no ledger
    d = g.reserve(item_id="i1", amount_rub=100, item_spent_today=0, total_spent_today=0)
    assert d.allowed
    assert d.reservation_id is None


def test_reservation_ledger_drops_expired_entries(tmp_path, monkeypatch):
    """Stale entries past RESERVATION_TTL_SEC must not inflate reserved_total."""
    import time

    from avito_ams import budget_guard
    from avito_ams.budget_guard import _ReservationLedger

    monkeypatch.setattr(budget_guard, "RESERVATION_TTL_SEC", 60)
    ledger = _ReservationLedger(tmp_path / "res.json")
    rid = ledger.reserve("i1", 150)
    assert ledger.reserved_total("i1") == 150

    # Simulate the holder dying: rewrite entry with ts in the past.
    import json
    data = json.loads((tmp_path / "res.json").read_text())
    data[rid]["ts"] = time.time() - 120  # 2 min ago, > 60s TTL
    (tmp_path / "res.json").write_text(json.dumps(data))

    assert ledger.reserved_total("i1") == 0  # expired entry dropped
