"""Budget protection — pre-flight check before any paid Avito API call."""

from __future__ import annotations

import fcntl
import json
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from avito_ams.budget_limits import BudgetLimits

# If a reserve() is never released within this window we assume the holder
# died (SIGKILL between reserve and the API call) and the entry is stale.
# Worst-case: a slow but legit API call past TTL gets double-counted in
# reserved_total — accept that vs leaking forever.
RESERVATION_TTL_SEC = 600  # 10 minutes


@dataclass
class Decision:
    allowed: bool
    reason: str
    requires_confirm: bool = False
    reservation_id: Optional[str] = None


class _ReservationLedger:
    """File-locked JSON store for in-flight budget reservations.

    Closes the TOCTOU window between `can_spend()` and the actual paid API
    call: amounts pending in the ledger are added to `*_spent_today` during
    the next `can_spend()`, so two concurrent runners can't both pass the
    check for the same budget.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def _locked(self):
        lock = self.path.with_suffix(".lock")
        with lock.open("w") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    def _read(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _write(self, data: dict) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(self.path)

    @staticmethod
    def _drop_expired(data: dict, now: float) -> dict:
        return {
            rid: r
            for rid, r in data.items()
            if now - r.get("ts", now) < RESERVATION_TTL_SEC
        }

    def reserve(self, item_id: str, amount: int) -> str:
        with self._locked():
            now = time.time()
            data = self._drop_expired(self._read(), now)
            rid = str(uuid.uuid4())
            data[rid] = {"item_id": item_id, "amount": amount, "ts": now}
            self._write(data)
            return rid

    def release(self, rid: str) -> None:
        with self._locked():
            data = self._read()
            data.pop(rid, None)
            self._write(data)

    def reserved_total(self, item_id: Optional[str] = None) -> int:
        data = self._drop_expired(self._read(), time.time())
        return sum(
            r["amount"]
            for r in data.values()
            if item_id is None or r["item_id"] == item_id
        )


class BudgetGuard:
    """Validates whether a paid action (VAS, autostrategy budget, CPXPromo bid, etc.)
    is allowed given current spending, balance, and CPA history."""

    def __init__(
        self,
        limits: BudgetLimits,
        promo_balance_rub: int,
        ledger: Optional["_ReservationLedger"] = None,
    ):
        self.limits = limits
        self.balance_rub = promo_balance_rub  # Promotion advance balance, not the main wallet
        self.ledger = ledger

    def can_spend(
        self,
        *,
        item_id: str,
        amount_rub: int,
        item_spent_today: int,
        total_spent_today: int,
        item_cpa_rub: Optional[int] = None,
        item_high_cpa_days: int = 0,
    ) -> Decision:
        if amount_rub <= 0:
            return Decision(False, "amount_rub must be > 0")

        # Balance check (need 2x to keep buffer for other operations)
        if self.balance_rub < amount_rub * 2:
            return Decision(
                False, f"BALANCE insufficient: {self.balance_rub}₽ < 2 × {amount_rub}₽"
            )

        # Per-item daily limit
        if item_spent_today + amount_rub > self.limits.max_daily_per_item_rub:
            return Decision(
                False,
                f"MAX_DAILY_PER_ITEM exceeded: {item_spent_today}+{amount_rub} > {self.limits.max_daily_per_item_rub}",
            )

        # Total daily limit
        if total_spent_today + amount_rub > self.limits.max_daily_total_rub:
            return Decision(
                False,
                f"MAX_DAILY_TOTAL exceeded: {total_spent_today}+{amount_rub} > {self.limits.max_daily_total_rub}",
            )

        # Stop-loss on chronic underperformance
        if (
            item_cpa_rub is not None
            and item_cpa_rub > self.limits.stop_loss_cpa_rub
            and item_high_cpa_days > self.limits.stop_loss_grace_days
        ):
            return Decision(
                False,
                f"STOP_LOSS triggered: CPA {item_cpa_rub} > {self.limits.stop_loss_cpa_rub} for {item_high_cpa_days} days",
            )

        # Auto-apply threshold
        requires_confirm = amount_rub > self.limits.auto_apply_limit_rub
        return Decision(True, "ok", requires_confirm=requires_confirm)

    def reserve(
        self,
        *,
        item_id: str,
        amount_rub: int,
        item_spent_today: int,
        total_spent_today: int,
        item_cpa_rub: Optional[int] = None,
        item_high_cpa_days: int = 0,
    ) -> Decision:
        """Reserve budget atomically. Returns Decision with reservation_id when
        allowed; caller MUST call `release(reservation_id)` once the action is
        applied (or failed) so the ledger doesn't leak.

        If no ledger is configured, falls back to plain `can_spend` (no
        TOCTOU protection — useful for tests / single-process callers).
        """
        if self.ledger is None:
            return self.can_spend(
                item_id=item_id,
                amount_rub=amount_rub,
                item_spent_today=item_spent_today,
                total_spent_today=total_spent_today,
                item_cpa_rub=item_cpa_rub,
                item_high_cpa_days=item_high_cpa_days,
            )
        item_reserved = self.ledger.reserved_total(item_id)
        total_reserved = self.ledger.reserved_total()
        decision = self.can_spend(
            item_id=item_id,
            amount_rub=amount_rub,
            item_spent_today=item_spent_today + item_reserved,
            total_spent_today=total_spent_today + total_reserved,
            item_cpa_rub=item_cpa_rub,
            item_high_cpa_days=item_high_cpa_days,
        )
        if not decision.allowed:
            return decision
        rid = self.ledger.reserve(item_id, amount_rub)
        return Decision(True, decision.reason, decision.requires_confirm, rid)

    def release(self, reservation_id: Optional[str]) -> None:
        if self.ledger and reservation_id:
            self.ledger.release(reservation_id)
