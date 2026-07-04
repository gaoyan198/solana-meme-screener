"""SOL/BTC spot prices via CoinGecko's free simple-price endpoint.

Used to benchmark paper positions: "would the same $100 in SOL or BTC have
done better?" Fails soft — a missing benchmark never blocks recording or
closing a position, it just shows as unknown in the report.
"""
from __future__ import annotations

import requests

from .log import get_logger

log = get_logger("bench")

_URL = "https://api.coingecko.com/api/v3/simple/price"


def sol_btc_prices() -> tuple[float | None, float | None]:
    try:
        r = requests.get(
            _URL,
            params={"ids": "solana,bitcoin", "vs_currencies": "usd"},
            timeout=15,
        )
        r.raise_for_status()
        j = r.json() or {}
        return (
            (j.get("solana") or {}).get("usd"),
            (j.get("bitcoin") or {}).get("usd"),
        )
    except Exception as e:  # noqa: BLE001
        log.warning("CoinGecko failed: %s", e)
        return (None, None)
