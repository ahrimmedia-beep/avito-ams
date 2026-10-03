import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from avito_ams.health_monitor import LOOP_ERRORS_ALERT_THRESHOLD, HealthMonitor


def _make(tmp_path, alert_fn=None, **kw):
    return HealthMonitor(
        state_file=tmp_path / "health.json",
        alert_fn=alert_fn or AsyncMock(),
        llm_api_key=kw.get("api_key", ""),
        disk_path=kw.get("disk_path", str(tmp_path)),
    )


def test_state_default_initialized(tmp_path):
    m = _make(tmp_path)
    assert m._state["counters"]["chats_processed"] == 0
    assert m._state["counters"]["escalated"] == 0


def test_inc_persists_counter(tmp_path):
    m = _make(tmp_path)
    m.inc("chats_processed")
    m.inc("chats_processed", 2)
    # Reload from disk
    m2 = _make(tmp_path)
    assert m2._state["counters"]["chats_processed"] == 3


def test_counter_resets_on_new_day(tmp_path):
    m = _make(tmp_path)
    m.inc("chats_processed", 5)
    # Manually rewrite date in state file to yesterday
    state = json.loads((tmp_path / "health.json").read_text())
    state["date"] = "2020-01-01"
    (tmp_path / "health.json").write_text(json.dumps(state))
    # New monitor should reset counters
    m2 = _make(tmp_path)
    assert m2._state["counters"]["chats_processed"] == 0


@pytest.mark.asyncio
async def test_avito_consecutive_errors_triggers_alert(tmp_path):
    alert = AsyncMock()
    m = _make(tmp_path, alert_fn=alert)
    await m.track_avito_error("HTTP 500")
    await m.track_avito_error("HTTP 500")
    assert alert.await_count == 0  # not yet
    await m.track_avito_error("HTTP 500")  # third -> alert
    assert alert.await_count == 1
    # Same alert not repeated
    await m.track_avito_error("HTTP 500")
    assert alert.await_count == 1


@pytest.mark.asyncio
async def test_avito_success_resets_counter(tmp_path):
    alert = AsyncMock()
    m = _make(tmp_path, alert_fn=alert)
    await m.track_avito_error("x")
    await m.track_avito_error("x")
    m.track_avito_success()
    assert m.avito_consecutive_errors == 0
    assert m.avito_alert_sent is False
    # Now 3 more errors should alert again
    await m.track_avito_error("x")
    await m.track_avito_error("x")
    await m.track_avito_error("x")
    assert alert.await_count == 1


@pytest.mark.asyncio
async def test_loop_errors_threshold_alerts(tmp_path):
    alert = AsyncMock()
    m = _make(tmp_path, alert_fn=alert)
    for i in range(LOOP_ERRORS_ALERT_THRESHOLD - 1):
        await m.track_loop_error(f"err {i}")
    assert alert.await_count == 0
    await m.track_loop_error("final")  # crosses threshold
    assert alert.await_count == 1
    # errors counter incremented
    assert m._state["counters"]["errors"] == LOOP_ERRORS_ALERT_THRESHOLD


@pytest.mark.asyncio
async def test_daily_digest_only_after_hour_and_once(tmp_path, monkeypatch):
    alert = AsyncMock()
    m = _make(tmp_path, alert_fn=alert)
    # Simulate "second day": first-run default sets last_digest_date=today to
    # skip empty first digest. Override here so the test runs the digest path.
    m._state["last_digest_date"] = ""
    m._state["counters"]["chats_processed"] = 7
    m._state["counters"]["escalated"] = 2

    # Pin "now" to 06:00 UTC = 09:00 Moscow time (too early)
    early = datetime(2026, 4, 28, 6, 0, tzinfo=timezone.utc)

    class PinnedDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return early.replace(tzinfo=tz) if tz else early

    monkeypatch.setattr("avito_ams.health_monitor.datetime", PinnedDT)
    await m.maybe_send_daily_digest()
    assert alert.await_count == 0  # too early

    # Pin to 08:00 UTC = 11:00 Moscow time
    later = datetime(2026, 4, 28, 8, 0, tzinfo=timezone.utc)

    class PinnedDT2(datetime):
        @classmethod
        def now(cls, tz=None):
            return later.replace(tzinfo=tz) if tz else later

    monkeypatch.setattr("avito_ams.health_monitor.datetime", PinnedDT2)
    await m.maybe_send_daily_digest()
    assert alert.await_count == 1
    assert "Chats processed:   7" in alert.await_args[0][0]
    assert "Escalated:         2" in alert.await_args[0][0]
    # Counters reset after digest
    assert m._state["counters"]["chats_processed"] == 0

    # Second call same day → no double digest
    await m.maybe_send_daily_digest()
    assert alert.await_count == 1


@pytest.mark.asyncio
async def test_llm_balance_check_is_noop(tmp_path):
    """The LLM provider has no public balance API, so the check must be a no-op."""
    alert = AsyncMock()
    m = _make(tmp_path, alert_fn=alert, api_key="sk-fake-key")
    # Multiple calls should never alert and never make HTTP calls.
    await m.check_openrouter_balance()
    await m.check_openrouter_balance()
    await m.check_openrouter_balance()
    assert alert.await_count == 0
    # State should not contain legacy balance keys.
    assert "last_balance_check_ts" not in m._state
    assert "last_balance_alert_ts" not in m._state


@pytest.mark.asyncio
async def test_legacy_state_keys_ignored_on_load(tmp_path):
    """Old state files with last_balance_* keys must load gracefully."""
    state_file = tmp_path / "health.json"
    state_file.write_text(
        json.dumps(
            {
                "date": "2099-01-01",  # forces counter reset to today
                "last_balance_check_ts": 12345.0,
                "last_balance_alert_ts": 67890.0,
                "last_disk_check_ts": 0.0,
                "last_disk_alert_ts": 0.0,
                "last_digest_date": "",
                "counters": {"chats_processed": 0, "escalated": 0,
                             "low_priority": 0, "spam": 0,
                             "messages_sent": 0, "errors": 0},
            }
        )
    )
    m = HealthMonitor(
        state_file=state_file,
        alert_fn=AsyncMock(),
        llm_api_key="",
        disk_path=str(tmp_path),
    )
    assert "last_balance_check_ts" not in m._state
    assert "last_balance_alert_ts" not in m._state


@pytest.mark.asyncio
async def test_disk_check_alerts_on_high_usage(tmp_path, monkeypatch):
    alert = AsyncMock()
    m = _make(tmp_path, alert_fn=alert)

    class FakeUsage:
        total = 100 * 10**9
        used = 90 * 10**9  # 90%
        free = 10 * 10**9

    monkeypatch.setattr("avito_ams.health_monitor.shutil.disk_usage", lambda _: FakeUsage)
    await m.check_disk_space()
    assert alert.await_count == 1


@pytest.mark.asyncio
async def test_disk_check_quiet_under_threshold(tmp_path, monkeypatch):
    alert = AsyncMock()
    m = _make(tmp_path, alert_fn=alert)

    class FakeUsage:
        total = 100 * 10**9
        used = 50 * 10**9  # 50%
        free = 50 * 10**9

    monkeypatch.setattr("avito_ams.health_monitor.shutil.disk_usage", lambda _: FakeUsage)
    await m.check_disk_space()
    assert alert.await_count == 0
