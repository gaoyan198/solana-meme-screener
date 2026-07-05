"""The screen pipeline: discover → gate → enrich → score → deep-check → hits.

Budgeted for a 5-minute GitHub Actions cron: a handful of GMGN list calls,
free Dexscreener/RugCheck enrichment for the best ~20 gated candidates, and
expensive per-token GMGN smart-money checks only for the top ~10 by
provisional score.
"""
from __future__ import annotations

import time

from .config import config
from .dexscreener import oldest_created_ts, token_pairs
from .geckoterminal import ath_price
from .gmgn import client
from .log import get_logger
from .models import Snapshot, first, num
from .rugcheck import summary as rug_summary
from .scoring import Scored, gate, score
from .state import state

log = get_logger("scan")

_RECENT_BUY_WINDOW_SEC = 2 * 3600


def discover() -> list[Snapshot]:
    """Pull several trending views and dedup into candidate snapshots.

    open_timestamp surfaces brand-new launches, swaps catches volume leaders,
    smartmoney catches wallet crowding — the union is the funnel top.
    """
    seen: dict[str, Snapshot] = {}
    views = [("1h", "open_timestamp"), ("5m", "swaps"), ("1h", "swaps"), ("1h", "smartmoney")]
    for period, orderby in views:
        for row in client.trending(period=period, orderby=orderby, limit=config.trending_limit):
            snap = Snapshot.from_row(row)
            if snap and snap.address not in seen:
                seen[snap.address] = snap
        time.sleep(config.request_delay_sec)
    log.info("Discovered %d unique tokens", len(seen))
    return list(seen.values())


def _deep_smart_check(snap: Snapshot) -> None:
    """Count distinct GMGN smart/KOL wallets holding + recent buyers."""
    now = time.time()
    holding: set[str] = set()
    recent: set[str] = set()
    for tag in config.smart_tags:
        for t in client.traders_by_tag(snap.address, tag):
            addr = first(t, "address", "wallet_address", "wallet")
            if not addr:
                continue
            balance = num(t, "balance", "amount_cur", "token_amount", "amount")
            if balance is not None and balance > 0:
                holding.add(str(addr))
                buy_ts = num(t, "start_holding_at", "buy_time")
                if buy_ts and (now - buy_ts) <= _RECENT_BUY_WINDOW_SEC:
                    recent.add(str(addr))
        time.sleep(config.request_delay_sec)
    snap.deep_checked = True
    snap.smart_holding = len(holding)
    snap.smart_recent_buys = len(recent)


def scan() -> list[Scored]:
    # Stage 1: cheap gate on raw trending rows.
    gated: list[Snapshot] = []
    for snap in discover():
        reason = gate(snap)
        if reason is None:
            gated.append(snap)
    log.info("%d candidates passed row-level gates", len(gated))

    # Stage 2: enrich with Dexscreener microstructure + RugCheck, then re-gate.
    # The budget is split across two rankings — smart-money hint AND raw 1h
    # volume — so a pure retail runner with zero KOL wallets (the ACM case)
    # still gets enriched and scored instead of starving behind hinted tokens.
    by_hint = sorted(gated, key=lambda s: (s.smart_hint, -(s.age_hours or 99)), reverse=True)
    by_vol = sorted(gated, key=lambda s: s.vol_h1 or 0, reverse=True)
    queue: list[Snapshot] = []
    for hinted, voluminous in zip(by_hint, by_vol):
        for snap in (hinted, voluminous):
            if snap not in queue:
                queue.append(snap)
    enriched: list[Snapshot] = []
    for snap in queue[: config.max_enrich]:
        pairs = token_pairs(snap.address)
        if pairs:
            snap.merge_pair(max(pairs, key=lambda p: ((p.get("liquidity") or {}).get("usd") or 0)))
            # True age = the OLDEST pool ever, so a fresh revival pool on a
            # months-dead token can't masquerade as a new launch.
            oldest = oldest_created_ts(pairs)
            if oldest:
                snap.created_ts = min(snap.created_ts or oldest, oldest)
        rug = rug_summary(snap.address)
        if rug:
            snap.rug_score_norm = num(rug, "score_normalised", "score_normalized")
        reason = gate(snap, strict=True)
        if reason:
            log.info("%s dropped post-enrich: %s", snap.symbol, reason)
            continue
        if snap.holders is not None:
            state.record_holders(snap.address, snap.holders)
        enriched.append(snap)
        time.sleep(config.request_delay_sec / 2)
    log.info("%d candidates survived enrichment", len(enriched))

    # Stage 3: provisional score decides who earns the expensive deep check.
    provisional = sorted(
        (score(s, state.holder_growth_rate(s.address, s.holders)) for s in enriched),
        key=lambda sc: sc.total,
        reverse=True,
    )
    results: list[Scored] = []
    for sc in provisional:
        if len(results) < config.max_deep_checks and not sc.snap.deep_checked:
            try:
                _deep_smart_check(sc.snap)
                sc = score(sc.snap, sc.holder_growth_rate)
            except Exception as e:  # noqa: BLE001
                log.warning("deep check failed for %s: %s", sc.snap.symbol, e)
        results.append(sc)

    results.sort(key=lambda sc: sc.total, reverse=True)
    for sc in results:
        log.info("%-12s %5.1f/100 (mom %5.1f)  mcap %s  %s", sc.snap.symbol[:12], sc.total,
                 sc.momentum_total,
                 f"${sc.snap.mcap_usd:,.0f}" if sc.snap.mcap_usd else "?",
                 " | ".join(f"{c.label} {c.points:.0f}/{c.max_points:.0f}" for c in sc.components))

    # Two alert tracks: the KOL composite, plus the smart-money-free momentum
    # score for retail runners the KOL track is structurally blind to.
    hits = [sc for sc in results if sc.total >= config.min_score]
    for sc in results:
        if sc not in hits and sc.momentum_total >= config.momentum_min_score:
            sc.track = "momentum"
            hits.append(sc)

    # Final gate, only on would-be alerts (GeckoTerminal budget): a coin far
    # below its lifetime high already pumped and finished dumping — the flat
    # chart that scores well on "timing" is a corpse, not a coil.
    fresh: list[Scored] = []
    for sc in hits:
        s = sc.snap
        if s.pair_address and s.price_usd:
            s.ath_price_usd = ath_price(s.pair_address)
            if s.ath_price_usd and s.price_usd < config.min_pct_of_ath * s.ath_price_usd:
                log.info("%s rejected: at %.0f%% of ATH — already pumped & dumped",
                         s.symbol, 100 * s.price_usd / s.ath_price_usd)
                continue
        fresh.append(sc)
    return fresh
