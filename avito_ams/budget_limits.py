"""Per-tenant budget limits with Pydantic validation.

In the full project this model is one section of the tenant config, which is
loaded from YAML. Only the budget section is published here.
"""

from __future__ import annotations

from typing import List

from pydantic import BaseModel, PositiveInt, conint, model_validator


class BudgetLimits(BaseModel):
    max_daily_per_item_rub: PositiveInt
    max_daily_total_rub: PositiveInt
    stop_loss_cpa_rub: PositiveInt
    target_cpa_rub: PositiveInt
    auto_apply_limit_rub: conint(ge=0)
    pacing_check_interval_hours: PositiveInt
    peak_hours: List[conint(ge=0, le=23)]
    peak_budget_pct: conint(ge=0, le=100)
    stop_loss_grace_days: PositiveInt = 3

    @model_validator(mode="after")
    def _check_target_below_stop_loss(self):
        if self.target_cpa_rub >= self.stop_loss_cpa_rub:
            raise ValueError(
                f"target_cpa_rub ({self.target_cpa_rub}) must be < stop_loss_cpa_rub ({self.stop_loss_cpa_rub})"
            )
        return self
