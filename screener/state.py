"""Persistent scan state — alert dedup + per-token holder history.

Kept as a small JSON file that GitHub Actions commits back to the repo. The
history is what turns a stateless 5-minute cron into a growth detector: the
holder-growth component compares this scan's holder count against the one
recorded ~1-4 scans ago.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .config import config
from .log import get_logger

log = get_logger("state")
STATE_FILE = Path(os.getenv("STATE_FILE", "state.json"))

_HISTORY_TTL_HOURS = 26     # a shade over max token age, then it's garbage
_HISTORY_MAX_POINTS = 24    # per token — 2h of 5-min scans is plenty
_MIN_BASELINE_AGE_SEC = 240 # ignore snapshots younger than ~1 scan for growth calc


class State:
    def __init__(self) -> None:
        self.alerted: dict[str, float] = {}                  # address -> last alert ts
        self.history: dict[str, list[list[float]]] = {}      # address -> [[ts, holders], ...]
        self.paper: dict = {"open": {}, "closed": []}        # paper-trading ledger (see paper.py)
        self.load()

    def load(self) -> None:
        if STATE_FILE.exists():
            try:
                data = json.loads(STATE_FILE.read_text())
                self.alerted = data.get("alerted", {})
                self.history = data.get("history", {})
                paper = data.get("paper") or {}
                self.paper = {"open": paper.get("open", {}), "closed": paper.get("closed", [])}
            except Exception as e:  # noqa: BLE001
                log.warning("Could not read state file: %s", e)
        self._prune()

    def _prune(self) -> None:
        now = time.time()
        # Dedup entries must outlive the paper holding period, otherwise a coin
        # could be re-alerted (and double-papered) while its position is open.
        ttl = max(config.alert_cooldown_hours, config.paper_hold_hours) * 3600
        self.alerted = {a: ts for a, ts in self.alerted.items() if now - ts < ttl}
        hist_ttl = _HISTORY_TTL_HOURS * 3600
        self.history = {
            a: pts[-_HISTORY_MAX_POINTS:]
            for a, pts in self.history.items()
            if pts and now - pts[-1][0] < hist_ttl
        }

    # --- dedup ------------------------------------------------------------
    def recently_alerted(self, address: str) -> bool:
        return address in self.alerted

    def mark_alerted(self, address: str) -> None:
        self.alerted[address] = time.time()

    # --- holder history ----------------------------------------------------
    def record_holders(self, address: str, holders: float) -> None:
        self.history.setdefault(address, []).append([time.time(), holders])

    def holder_growth_rate(self, address: str, current: float | None) -> float | None:
        """Fractional holder growth per hour vs the oldest usable snapshot.

        None when there's no baseline yet (first sighting) — the scorer treats
        that as neutral rather than penalising fresh discoveries.
        """
        if current is None:
            return None
        now = time.time()
        usable = [p for p in self.history.get(address, []) if now - p[0] >= _MIN_BASELINE_AGE_SEC]
        if not usable:
            return None
        ts, prev = usable[0]
        dt_hours = (now - ts) / 3600
        if prev <= 0 or dt_hours <= 0:
            return None
        return (current - prev) / prev / dt_hours

    def save(self) -> None:
        self._prune()
        STATE_FILE.write_text(
            json.dumps(
                {"alerted": self.alerted, "history": self.history, "paper": self.paper},
                indent=2,
            )
        )


state = State()
