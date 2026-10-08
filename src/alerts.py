"""Telegram sending and de-duplication.

Two kinds of alert:
- **Condition** (`Alert.condition=True`, e.g. "drift:BTC"): sent once while the
  condition holds, and again only after it clears and comes back. A condition
  clears when its family (the part before ":") was evaluated this run and the
  key was not raised.
- **Event** (exits, breakouts, reminders): sent once per key, ever. Keys carry
  their own period ("rebalance:2026-10", "breakout:SOL-USDC:2026-10-06").

Alerts that fail to send stay in `pending` and are retried next run.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable

import httpx

HISTORY_KEEP = 50
EVENT_KEEP_DAYS = 120


@dataclass(frozen=True)
class Alert:
    key: str
    text: str
    condition: bool = False

    @property
    def family(self) -> str:
        return self.key.split(":", 1)[0]


def load_state(path: Path) -> dict:
    try:
        state = json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        state = {}
    for k in ("active", "events"):
        state.setdefault(k, {})
    for k in ("pending", "history"):
        state.setdefault(k, [])
    return state


def select(alerts: Iterable[Alert], state: dict, evaluated: Iterable[str],
           now: datetime) -> list[Alert]:
    """The alerts to send now. Updates `state` for conditions that cleared or fired.

    `evaluated` names the condition families checked this run; active keys of
    other families are left alone (a skipped check is not a cleared one).
    """
    now_s = stamp(now)
    alerts = list(alerts)
    raised = {a.key for a in alerts if a.condition}
    evaluated = set(evaluated)
    for key in list(state["active"]):
        if key.split(":", 1)[0] in evaluated and key not in raised:
            del state["active"][key]

    to_send = []
    for a in alerts:
        book = state["active"] if a.condition else state["events"]
        if a.key in book or any(p["key"] == a.key for p in state["pending"]):
            continue
        book[a.key] = now_s
        to_send.append(a)

    cutoff = stamp(now - timedelta(days=EVENT_KEEP_DAYS))
    state["events"] = {k: v for k, v in state["events"].items() if v >= cutoff}
    return to_send


def deliver(alerts: list[Alert], state: dict, send: Callable[[str], bool],
            now: datetime) -> list[Alert]:
    """Send pending alerts then new ones. Returns those that failed (kept pending)."""
    queue = [Alert(**p) for p in state["pending"]] + alerts
    failed = []
    for a in queue:
        if send(a.text):
            state["history"].append({"time": stamp(now), "key": a.key, "text": a.text})
        else:
            failed.append(a)
    state["pending"] = [asdict(a) for a in failed]
    state["history"] = state["history"][-HISTORY_KEEP:]
    return failed


def save_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n")


def stamp(now: datetime) -> str:
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------- senders


class TelegramError(RuntimeError):
    pass


def telegram_sender(token: str | None = None, chat_id: str | None = None,
                    timeout: float = 10) -> Callable[[str], bool]:
    """A sender for the bot in TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID."""
    token = token or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise TelegramError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set "
                            "(or run with --dry-run)")
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    def send(text: str) -> bool:
        try:
            r = httpx.post(url, timeout=timeout, json={
                "chat_id": chat_id, "text": text, "disable_web_page_preview": True})
            return r.status_code == 200 and r.json().get("ok", False)
        except (httpx.HTTPError, ValueError):
            return False

    return send


def print_sender(text: str) -> bool:
    print("─" * 40)
    print(text)
    return True
