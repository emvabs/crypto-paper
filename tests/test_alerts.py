from datetime import datetime, timedelta, timezone

from src import alerts
from src.alerts import Alert

T0 = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)


def fresh():
    return alerts.load_state(alerts.Path("/nonexistent/alert_state.json"))


def keys(sent):
    return [a.key for a in sent]


def test_condition_sent_once_while_it_holds():
    s = fresh()
    drift = Alert("drift:BTC", "BTC out", condition=True)
    assert keys(alerts.select([drift], s, {"drift"}, T0)) == ["drift:BTC"]
    assert alerts.select([drift], s, {"drift"}, T0) == []


def test_condition_resent_after_it_clears():
    s = fresh()
    drift = Alert("drift:BTC", "BTC out", condition=True)
    alerts.select([drift], s, {"drift"}, T0)
    alerts.select([], s, {"drift"}, T0)  # back in band
    assert keys(alerts.select([drift], s, {"drift"}, T0)) == ["drift:BTC"]


def test_unevaluated_family_does_not_clear():
    s = fresh()
    drift = Alert("drift:BTC", "BTC out", condition=True)
    alerts.select([drift], s, {"drift"}, T0)
    alerts.select([], s, {"floor"}, T0)  # drift not checked (bad prices)
    assert alerts.select([drift], s, {"drift"}, T0) == []


def test_event_sent_once_ever():
    s = fresh()
    ev = Alert("exit:7:1", "T1 hit")
    assert keys(alerts.select([ev], s, set(), T0)) == ["exit:7:1"]
    assert alerts.select([ev], s, set(), T0 + timedelta(days=30)) == []


def test_old_events_are_pruned():
    s = fresh()
    alerts.select([Alert("rebalance:2026-01", "x")], s, set(), T0)
    alerts.select([], s, set(), T0 + timedelta(days=alerts.EVENT_KEEP_DAYS + 1))
    assert s["events"] == {}


def test_failed_send_is_retried_next_run_and_not_duplicated():
    s = fresh()
    ev = Alert("exit:7:1", "T1 hit")
    failed = alerts.deliver(alerts.select([ev], s, set(), T0), s, lambda t: False, T0)
    assert keys(failed) == ["exit:7:1"] and s["pending"][0]["key"] == "exit:7:1"
    # next run: the same event is raised again but only the pending copy goes out
    sent = []
    new = alerts.select([ev], s, set(), T0)
    assert new == []
    alerts.deliver(new, s, lambda t: sent.append(t) or True, T0)
    assert sent == ["T1 hit"] and s["pending"] == []
    assert s["history"][-1]["key"] == "exit:7:1"


def test_history_is_capped():
    s = fresh()
    many = [Alert(f"e:{i}", str(i)) for i in range(alerts.HISTORY_KEEP + 10)]
    alerts.deliver(alerts.select(many, s, set(), T0), s, lambda t: True, T0)
    assert len(s["history"]) == alerts.HISTORY_KEEP


def test_save_and_load_round_trip(tmp_path):
    s = fresh()
    alerts.select([Alert("drift:ETH", "x", condition=True)], s, {"drift"}, T0)
    alerts.save_state(tmp_path / "a.json", s)
    assert alerts.load_state(tmp_path / "a.json")["active"] == s["active"]


def test_missing_telegram_secrets_raise():
    import os
    import pytest
    env = {k: os.environ.pop(k) for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
           if k in os.environ}
    try:
        with pytest.raises(alerts.TelegramError):
            alerts.telegram_sender()
    finally:
        os.environ.update(env)
