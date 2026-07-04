"""GMGN.ai data client (ported from solana-kol-bot).

GMGN's *public* data endpoints (trending, top traders) are not an official
product — they are the same JSON endpoints gmgn.ai's own web app calls, and
they sit behind Cloudflare. We use curl_cffi's browser TLS impersonation to
get through without a headless browser. If datacenter IPs (e.g. GitHub
Actions) get 403'd, set GMGN_PROXY to a residential proxy.

Every method returns plain parsed JSON (the unwrapped `data` field) or None on
failure — callers must interpret fields defensively, since GMGN occasionally
renames them.
"""
from __future__ import annotations

import time
from typing import Any

from curl_cffi import requests as cffi

from .config import config
from .log import get_logger

log = get_logger("gmgn")

_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "en-US,en;q=0.9",
    "referer": "https://gmgn.ai/",
    "origin": "https://gmgn.ai",
}


class GmgnClient:
    def __init__(self) -> None:
        self._session = cffi.Session()
        self._proxies = (
            {"http": config.gmgn_proxy, "https": config.gmgn_proxy}
            if config.gmgn_proxy
            else None
        )

    def _get(self, path: str, params: dict | None = None, retries: int = 3) -> Any | None:
        url = f"{config.gmgn_base}{path}"
        headers = dict(_HEADERS)
        if config.gmgn_api_key:
            headers["x-route-key"] = config.gmgn_api_key
        for attempt in range(1, retries + 1):
            try:
                r = self._session.get(
                    url,
                    params=params,
                    headers=headers,
                    impersonate="chrome",
                    proxies=self._proxies,
                    timeout=20,
                )
                if r.status_code in (403, 429):
                    wait = 3 * attempt if r.status_code == 429 else 2 * attempt
                    log.warning("GMGN %d on %s — backing off %ds (attempt %d/%d)",
                                r.status_code, path, wait, attempt, retries)
                    time.sleep(wait)
                    continue
                r.raise_for_status()
                body = r.json()
                # GMGN wraps as {"code":0,"msg":"success","data":{...}}
                if isinstance(body, dict) and "data" in body:
                    if body.get("code") not in (0, None):
                        log.warning("GMGN non-zero code on %s: %s", path, body.get("msg"))
                    return body["data"]
                return body
            except Exception as e:  # noqa: BLE001
                log.warning("GMGN request failed (%s) attempt %d/%d: %s", path, attempt, retries, e)
                time.sleep(1.5 * attempt)
        return None

    # --- discovery --------------------------------------------------------
    def trending(self, period: str = "1h", orderby: str = "swaps", limit: int = 100) -> list[dict]:
        """Ranked tokens for a time bucket. orderby e.g. smartmoney, swaps, open_timestamp."""
        data = self._get(
            f"/defi/quotation/v1/rank/sol/swaps/{period}",
            params={
                "orderby": orderby,
                "direction": "desc",
                "limit": limit,
                "filters[]": "not_honeypot",
            },
        )
        return _as_list(data, keys=("rank", "list", "tokens"))

    # --- per-token --------------------------------------------------------
    def traders_by_tag(self, token_address: str, tag: str) -> list[dict]:
        """Traders in a GMGN cohort for a token, with cost basis + holdings.

        tag is a GMGN wallet cohort: 'renowned' (KOL/famous wallets) or
        'smart_degen' (smart money). Each item has avg_cost, balance/amount_cur
        (current holding) and start_holding_at (buy time).
        """
        data = self._get(
            f"/vas/api/v1/token_traders/sol/{token_address}",
            params={"orderby": "profit", "tag": tag},
        )
        return _as_list(data, keys=("list", "traders"))

    def token_info(self, token_address: str) -> dict | None:
        data = self._get(f"/defi/quotation/v1/tokens/sol/{token_address}")
        if isinstance(data, dict):
            return data.get("token", data)
        return None


def _as_list(data: Any, keys: tuple[str, ...]) -> list[dict]:
    """GMGN sometimes returns a bare list, sometimes {key: [...]}. Normalise."""
    if data is None:
        return []
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for k in keys:
            v = data.get(k)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
    return []


client = GmgnClient()
