"""RugCheck public summary API — free, key-less.

One extra safety opinion on top of GMGN's row flags. `score_normalised` is
0–100 where HIGHER = riskier. Fails soft: a missing report never blocks a
scan, it just earns the token neutral (half) safety credit.
"""
from __future__ import annotations

import requests

from .log import get_logger

log = get_logger("rug")

_BASE = "https://api.rugcheck.xyz/v1/tokens"


def summary(mint: str) -> dict | None:
    """{"score_normalised": int, "risks": [...]} or None if unavailable."""
    try:
        r = requests.get(f"{_BASE}/{mint}/report/summary", timeout=15)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        body = r.json()
        return body if isinstance(body, dict) else None
    except Exception as e:  # noqa: BLE001
        log.warning("RugCheck failed for %s: %s", mint[:8], e)
        return None
