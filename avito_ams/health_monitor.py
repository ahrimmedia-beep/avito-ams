"""Health monitoring for the chat bot. All checks run inside the bot, no external services.

What it watches:
- Avito API: 3 consecutive errors -> Telegram alert
- Loop errors: >10 per hour -> Telegram alert
- LLM balance: DISABLED (the LLM provider has no public balance API, so it is
  monitored manually in the provider dashboard)
- Disk space on /var: >85% -> Telegram alert (cooldown 24h)
- Daily digest at 10:00 Moscow time: counters of processed/escalated/errors

State is persisted to a JSON file (counters reset daily).
"""

from __future__ import annotations

import json
import logging
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

if TYPE_CHECKING:
    import httpx

logger = logging.getLogger(__name__)

DAILY_DIGEST_HOUR_UTC = 7  # 10:00 Moscow time = 07:00 UTC
DISK_ALERT_PCT = 85
LOOP_ERRORS_ALERT_THRESHOLD = 10
LOOP_ERRORS_WINDOW_SEC = 3600
DISK_CHECK_INTERVAL_SEC = 86400  # 1 day
ALERT_COOLDOWN_SEC = 86400  # 24h between repeat alerts of same kind

AlertFn = Callable[[str], Awaitable[None]]


class HealthMonitor:
    def __init__(
        self,
        state_file: Path,
        alert_fn: AlertFn,
        llm_api_key: str = "",
        disk_path: str = "/var",
        http_client: Optional[httpx.Client] = None,
    ):
        self.state_file = state_file
        self.alert_fn = alert_fn
        # Currently unused: the LLM provider has no public balance API.
        # Kept for backward compatibility with callers; may be wired up
        # again if a future LLM provider exposes a balance endpoint.
        self.llm_api_key = llm_api_key
        self.disk_path = disk_path
        self._http = http_client  # not used currently; kept for future re-use
        self._state = self._load()
        self._llm_balance_disabled_logged = False
        # In-memory transient counters (reset on process restart, which is fine)
        self.avito_consecutive_errors = 0
        self.avito_alert_sent = False
        self.loop_error_timestamps: list[float] = []
        self.loop_alert_sent_at: float = 0.0

    # --- state persistence ---

    def _today_utc(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _default_state(self) -> dict:
        return {
            "date": self._today_utc(),
            "last_disk_check_ts": 0.0,
            "last_disk_alert_ts": 0.0,
            "last_digest_date": "",
            "counters": {
                "chats_processed": 0,
                "escalated": 0,
                "low_priority": 0,
                "spam": 0,
                "messages_sent": 0,
                "errors": 0,
            },
        }

    def _load(self) -> dict:
        defaults = self._default_state()
        if not self.state_file.exists():
            # First-run: skip today's digest (counters are zero, would be useless).
            # Daily digest starts tomorrow.
            defaults["last_digest_date"] = defaults["date"]
            return defaults
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            defaults["last_digest_date"] = defaults["date"]
            return defaults
        # Merge with defaults so new keys appear; extra keys from older state
        # files (e.g. last_balance_check_ts) are silently ignored on save
        # because we only persist self._state which is built from defaults.
        merged = {**defaults, **{k: v for k, v in data.items() if k in defaults}}
        # Reset counters on new UTC day
        if merged.get("date") != defaults["date"]:
            merged["date"] = defaults["date"]
            merged["counters"] = defaults["counters"]
        # Ensure all counter keys present
        for k, v in defaults["counters"].items():
            merged["counters"].setdefault(k, v)
        return merged

    def _save(self) -> None:
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(
                json.dumps(self._state, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as e:
            logger.warning("HealthMonitor save failed: %s", e)

    # --- counters (called from process_chat / main_loop) ---

    def inc(self, key: str, n: int = 1) -> None:
        self._state["counters"][key] = self._state["counters"].get(key, 0) + n
        self._save()

    # --- Avito API error tracking ---

    def track_avito_success(self) -> None:
        if self.avito_consecutive_errors > 0:
            self.avito_consecutive_errors = 0
            self.avito_alert_sent = False

    async def track_avito_error(self, error_label: str) -> None:
        self.avito_consecutive_errors += 1
        if self.avito_consecutive_errors >= 3 and not self.avito_alert_sent:
            await self._safe_alert(
                f"🔴 Avito API: {self.avito_consecutive_errors} errors in a row "
                f"({error_label}).\nCheck the plan / scope / keys."
            )
            self.avito_alert_sent = True

    # --- Loop error rate ---

    async def track_loop_error(self, error_text: str) -> None:
        now = time.time()
        cutoff = now - LOOP_ERRORS_WINDOW_SEC
        self.loop_error_timestamps = [t for t in self.loop_error_timestamps if t > cutoff]
        self.loop_error_timestamps.append(now)
        self.inc("errors")
        if (
            len(self.loop_error_timestamps) >= LOOP_ERRORS_ALERT_THRESHOLD
            and (now - self.loop_alert_sent_at) > LOOP_ERRORS_WINDOW_SEC
        ):
            await self._safe_alert(
                f"⚠️ Chat bot: {len(self.loop_error_timestamps)} loop errors in the last hour.\n"
                f"Last one: {error_text[:200]}"
            )
            self.loop_alert_sent_at = now

    # --- LLM balance (DISABLED) ---

    async def check_openrouter_balance(self) -> None:
        """No-op. The LLM provider has no public balance API.

        Kept for backward compatibility with existing callers (e.g. tick()).
        Logs once per process to remind operator to monitor manually.
        """
        if not self._llm_balance_disabled_logged:
            logger.info(
                "LLM balance check disabled, monitor it manually "
                "in the provider dashboard"
            )
            self._llm_balance_disabled_logged = True

    # --- Disk space ---

    async def check_disk_space(self) -> None:
        now = time.time()
        if (now - self._state["last_disk_check_ts"]) < DISK_CHECK_INTERVAL_SEC:
            return
        try:
            usage = shutil.disk_usage(self.disk_path)
            used_pct = (usage.used / usage.total) * 100
            self._state["last_disk_check_ts"] = now
            self._save()
            if used_pct > DISK_ALERT_PCT and (
                now - self._state["last_disk_alert_ts"]
            ) > ALERT_COOLDOWN_SEC:
                await self._safe_alert(
                    f"💾 Server disk {self.disk_path}: {used_pct:.0f}% used.\n"
                    f"Clean up old logs or grow the disk."
                )
                self._state["last_disk_alert_ts"] = now
                self._save()
        except Exception as e:
            logger.warning("Disk space check failed: %s", e)

    # --- Daily digest ---

    async def maybe_send_daily_digest(self) -> None:
        now_utc = datetime.now(timezone.utc)
        today = now_utc.strftime("%Y-%m-%d")
        if self._state.get("last_digest_date") == today:
            return
        if now_utc.hour < DAILY_DIGEST_HOUR_UTC:
            return  # too early, wait for 10:00 Moscow time

        c = self._state["counters"]
        msg = (
            f"📊 Chat bot report for {today}\n"
            f"────────────\n"
            f"Chats processed:   {c.get('chats_processed', 0)}\n"
            f"Escalated:         {c.get('escalated', 0)}\n"
            f"Low priority:      {c.get('low_priority', 0)}\n"
            f"Spam candidates:   {c.get('spam', 0)}\n"
            f"Messages sent:     {c.get('messages_sent', 0)}\n"
            f"Loop errors:       {c.get('errors', 0)}"
        )
        await self._safe_alert(msg)
        self._state["last_digest_date"] = today
        # reset counters for new day
        self._state["counters"] = self._default_state()["counters"]
        self._save()

    # --- Periodic tick, called from main_loop ---

    async def tick(self) -> None:
        """Call after each polling cycle. Runs all periodic checks."""
        await self.check_openrouter_balance()
        await self.check_disk_space()
        await self.maybe_send_daily_digest()

    # --- internal ---

    async def _safe_alert(self, text: str) -> None:
        try:
            await self.alert_fn(text)
        except Exception as e:
            logger.warning("Health alert send failed: %s", e)
