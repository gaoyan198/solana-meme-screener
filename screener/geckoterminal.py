"""GeckoTerminal public OHLCV API — free, key-less.

Used to find a pool's lifetime high ("ATH") in USD: max candle high across
5-minute candles (fine resolution, ~3.5 days of coverage) and hourly candles
(coarse, ~41 days). Young memecoins fit comfortably. Fails soft — no data
just renders as "?" in alerts/reports.

Public rate limit is ~30 calls/min; callers should only fetch ATH for the
handful of tokens being alerted or reported, never the whole scan funnel.
"""
from __future__ import annotations

import time

import requests

from .log import get_logger

log = get_logger("gecko")

_BASE = "https://api.geckoterminal.com/api/v2/networks/solana/pools"


def ath_price(pool_address: str) -> float | None:
    """Highest traded price (USD) we can see for this pool, or None."""
    highs: list[float] = []
    for timeframe, params in (
        ("minute", {"aggregate": 5, "limit": 1000, "currency": "usd"}),
        ("hour", {"aggregate": 1, "limit": 1000, "currency": "usd"}),
    ):
        try:
            r = requests.get(f"{_BASE}/{pool_address}/ohlcv/{timeframe}",
                             params=params, timeout=15)
            if r.status_code == 404:
                continue
            r.raise_for_status()
            candles = (((r.json() or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
            highs += [float(c[2]) for c in candles if isinstance(c, (list, tuple)) and len(c) > 2 and c[2]]
        except Exception as e:  # noqa: BLE001
            log.warning("GeckoTerminal %s failed for %s: %s", timeframe, pool_address[:8], e)
        time.sleep(0.25)
    return max(highs) if highs else None
