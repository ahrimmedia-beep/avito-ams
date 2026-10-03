"""Pacing — distribute daily budget evenly across 24 hours so ads stay live all day,
with a peak-hours boost (e.g. 19:00-22:00 gets 30% of daily budget)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Literal

PACING_THRESHOLD_PCT = 30  # +/- 30% before adjusting


@dataclass
class BidAdjustment:
    action: Literal["increase", "decrease", "hold"]
    new_bid: int
    reason: str


def target_spend_by_hour(
    *,
    daily_budget: int,
    hour: int,
    peak_hours: List[int],
    peak_pct: int,
) -> int:
    """Cumulative target spend by end of given hour (0-23).

    Before the first peak hour is reached the distribution is uniform
    (daily_budget / 24 per hour).  Once the peak window starts, off-peak
    hours are charged at the adjusted off-peak rate and peak hours at the
    boosted rate so that the day ends exactly at daily_budget.
    """
    if not 0 <= hour <= 23:
        raise ValueError(f"hour must be 0-23, got {hour}")

    # Edge cases
    if hour == 0:
        return 0
    if hour == 23:
        return daily_budget

    peak_set = set(peak_hours)
    peak_count = len(peak_hours)
    off_peak_count = 24 - peak_count
    if off_peak_count <= 0:
        raise ValueError("peak_hours must leave at least one off-peak hour")

    peak_total = daily_budget * peak_pct // 100
    off_peak_total = daily_budget - peak_total
    per_off_peak = off_peak_total // off_peak_count
    per_peak = peak_total // peak_count if peak_count > 0 else 0

    first_peak = min(peak_set) if peak_set else 24

    if hour < first_peak:
        # Uniform distribution before any peak hour
        return daily_budget * hour // 24

    # Past the start of peak window: use adjusted rates
    pre_peak_spend = first_peak * per_off_peak  # off-peak hours before peak window
    cumulative = pre_peak_spend
    for h in range(first_peak, hour + 1):
        if h in peak_set:
            cumulative += per_peak
        else:
            cumulative += per_off_peak
    return cumulative


def recommend_bid_adjustment(
    *,
    actual_spent: int,
    target_spent: int,
    current_bid: int,
    threshold_pct: int = PACING_THRESHOLD_PCT,
) -> BidAdjustment:
    """If actual is far from target, recommend adjusting bid (CPXPromo)."""
    if target_spent == 0:
        return BidAdjustment("hold", current_bid, "no target yet")

    deviation_pct = (actual_spent - target_spent) * 100 // target_spent

    if deviation_pct > threshold_pct:
        # over-spending — slow down
        new_bid = max(1, current_bid - max(1, current_bid * 20 // 100))
        return BidAdjustment(
            "decrease",
            new_bid,
            f"actual {actual_spent} > target {target_spent} (+{deviation_pct}%)",
        )
    if deviation_pct < -threshold_pct:
        # under-spending — speed up
        new_bid = current_bid + max(1, current_bid * 20 // 100)
        return BidAdjustment(
            "increase",
            new_bid,
            f"actual {actual_spent} < target {target_spent} ({deviation_pct}%)",
        )
    return BidAdjustment("hold", current_bid, f"within ±{threshold_pct}%")
