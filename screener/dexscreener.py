"""Dexscreener public API — free, key-less, no Cloudflare.

Used to enrich candidates with clean market microstructure GMGN's trending
rows don't reliably carry: 5-min vs 1-hour volume, buy/sell transaction
counts, and short-horizon price changes. Rate limit is ~300 req/min, far
above what a 5-minute cron needs.
"""
from __future__ import annotations

import requests

from .log import get_logger

log = get_logger("dexs")

_BASE = "https://api.dexscreener.com/latest/dex/tokens"


def best_pair(mint: str) -> dict | None:
    """The most liquid Solana pair for a mint, or None."""
    try:
        r = requests.get(f"{_BASE}/{mint}", timeout=15)
        r.raise_for_status()
        pairs = (r.json() or {}).get("pairs") or []
    except Exception as e:  # noqa: BLE001
        log.warning("Dexscreener failed for %s: %s", mint[:8], e)
        return None
    sol_pairs = [p for p in pairs if isinstance(p, dict) and p.get("chainId") == "solana"]
    if not sol_pairs:
        return None
    return max(sol_pairs, key=lambda p: ((p.get("liquidity") or {}).get("usd") or 0))
