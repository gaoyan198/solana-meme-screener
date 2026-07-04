"""Candidate snapshot + defensive field extractors for GMGN's (occasionally-renamed) JSON.

All the "which key is it this week" guesswork lives here so the scoring logic
stays readable. If GMGN changes a field, adjust the key tuples below — the
`main.py dump` command prints raw JSON to help you find the new names.

A Snapshot starts from a GMGN trending row; `merge_pair` overlays Dexscreener's
cleaner market microstructure (short-horizon volume, buy/sell tx counts, price
change), which GMGN rows don't reliably carry.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


def num(d: dict, *keys: str) -> float | None:
    """First key that holds a parseable number."""
    for k in keys:
        v = d.get(k)
        if v in (None, "", "null"):
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return None


def first(d: dict, *keys: str) -> Any:
    for k in keys:
        v = d.get(k)
        if v not in (None, "", "null"):
            return v
    return None


@dataclass
class Snapshot:
    address: str
    symbol: str
    name: str
    created_ts: float | None
    price_usd: float | None
    mcap_usd: float | None
    liquidity_usd: float | None
    holders: float | None
    # smart money hints straight off the trending row
    smart_hint: float                 # smart_degen_count + renowned_count
    smart_buys_h1: float | None
    smart_sells_h1: float | None
    # distribution / safety flags (None = GMGN didn't say)
    top10_rate: float | None          # 0..1
    insider_rate: float | None        # 0..1 ("rat trader" share)
    sniper_rate: float | None         # 0..1
    burn_ratio: float | None          # 0..1 of LP burned
    mint_renounced: bool | None
    honeypot: bool | None
    # market microstructure (mostly filled by merge_pair)
    vol_m5: float | None = None
    vol_h1: float | None = None
    buys_m5: float | None = None
    sells_m5: float | None = None
    change_m5_pct: float | None = None
    change_h1_pct: float | None = None
    # deep smart-money check (filled by scanner for top candidates)
    deep_checked: bool = False
    smart_holding: int = 0
    smart_recent_buys: int = 0
    # rugcheck (filled by scanner)
    rug_score_norm: float | None = None   # 0-100, higher = riskier
    raw: dict = field(repr=False, default_factory=dict)

    # --- construction -----------------------------------------------------
    @classmethod
    def from_row(cls, row: dict) -> "Snapshot | None":
        addr = first(row, "address", "token_address", "id", "mint")
        if not addr:
            return None
        renowned = num(row, "renowned_count") or 0
        smart = num(row, "smart_degen_count", "smart_money", "smartmoney") or 0
        return cls(
            address=str(addr),
            symbol=str(first(row, "symbol", "token_symbol") or "?"),
            name=str(first(row, "name", "token_name") or ""),
            created_ts=num(row, "open_timestamp", "creation_timestamp", "created_timestamp",
                           "pool_creation_timestamp"),
            price_usd=num(row, "price", "usd_price", "price_usd"),
            mcap_usd=num(row, "market_cap", "usd_market_cap", "mkt_cap", "fdv"),
            liquidity_usd=num(row, "liquidity", "liquidity_usd", "usd_liquidity"),
            holders=num(row, "holder_count", "holders", "holder"),
            smart_hint=renowned + smart,
            smart_buys_h1=num(row, "smart_buy_1h", "smart_buy_24h"),
            smart_sells_h1=num(row, "smart_sell_1h", "smart_sell_24h"),
            top10_rate=_rate(num(row, "top_10_holder_rate", "top10_holder_rate", "top_holder_rate")),
            insider_rate=_rate(num(row, "rat_trader_amount_rate", "insider_rate", "insider_percentage")),
            sniper_rate=_rate(num(row, "sniper_rate", "snipers_rate")),
            burn_ratio=_rate(num(row, "burn_ratio", "burn_percentage")),
            mint_renounced=_flag(first(row, "renounced_mint", "is_renounced_mint", "renounced")),
            honeypot=_flag(first(row, "is_honeypot", "honeypot")),
            vol_h1=num(row, "volume", "volume_1h", "volume_u"),
            change_h1_pct=num(row, "price_change_percent1h", "price_change_percent",
                              "price_change_1h"),
            raw=row,
        )

    def merge_pair(self, pair: dict) -> None:
        """Overlay a Dexscreener pair; its microstructure wins where present."""
        vol = pair.get("volume") or {}
        txns = pair.get("txns") or {}
        m5 = txns.get("m5") or {}
        change = pair.get("priceChange") or {}
        liq = pair.get("liquidity") or {}
        self.vol_m5 = num(vol, "m5") if num(vol, "m5") is not None else self.vol_m5
        self.vol_h1 = num(vol, "h1") if num(vol, "h1") is not None else self.vol_h1
        self.buys_m5 = num(m5, "buys")
        self.sells_m5 = num(m5, "sells")
        self.change_m5_pct = num(change, "m5") if num(change, "m5") is not None else self.change_m5_pct
        self.change_h1_pct = num(change, "h1") if num(change, "h1") is not None else self.change_h1_pct
        self.price_usd = num(pair, "priceUsd") or self.price_usd
        self.mcap_usd = num(pair, "marketCap", "fdv") or self.mcap_usd
        self.liquidity_usd = num(liq, "usd") or self.liquidity_usd
        created_ms = num(pair, "pairCreatedAt")
        if self.created_ts is None and created_ms:
            self.created_ts = created_ms / 1000

    # --- derived ----------------------------------------------------------
    @property
    def age_hours(self) -> float | None:
        if not self.created_ts:
            return None
        return max(0.0, (time.time() - self.created_ts) / 3600)

    @property
    def vol_accel(self) -> float | None:
        """5-min volume annualised to 1h, over actual 1h volume. >1 = accelerating."""
        if self.vol_m5 is None or not self.vol_h1:
            return None
        return (self.vol_m5 * 12) / self.vol_h1

    @property
    def buy_ratio_m5(self) -> float | None:
        if self.buys_m5 is None or self.sells_m5 is None:
            return None
        total = self.buys_m5 + self.sells_m5
        return self.buys_m5 / total if total else None

    @property
    def txns_m5(self) -> float | None:
        if self.buys_m5 is None or self.sells_m5 is None:
            return None
        return self.buys_m5 + self.sells_m5

    @property
    def holders_per_hour(self) -> float | None:
        if self.holders is None or self.age_hours is None:
            return None
        return self.holders / max(self.age_hours, 0.25)


def _rate(v: float | None) -> float | None:
    """Normalise a share that may arrive as 0..1 or 0..100."""
    if v is None:
        return None
    return v / 100 if v > 1.5 else v


def _flag(v: Any) -> bool | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes")
    return None
