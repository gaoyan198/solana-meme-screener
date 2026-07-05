"""The deciphered stalk.fun-style scoring engine.

Two stages:

1. `gate()` — hard binary rug/relevance filters. Any fail and the token is
   dropped without a score (too old, too illiquid, too concentrated, already
   ran, dead flow).

2. `score()` — a weighted 0-100 composite over the five things stalk.fun's
   "Print Scan" says it analyses (volume, holders, KOL/smart-money activity,
   price variation) plus the distribution checks their own guides tell users
   to do by hand:

     Momentum       20  volume acceleration + buy/sell ratio
     Holder growth  15  holders/hour since launch + growth since last scan
     Smart money    35  KOL/smart wallets HOLDING (insider-crowding thesis),
                        buy recency, and net smart flow (holding ≠ dumping)
     Safety         20  top-10 %, insider %, RugCheck, renounce/burn
     Entry timing   10  "coiling" beats "already ran"

Smart money is the heaviest component by design (v2, 2026-07-05): several
smart wallets *still holding* a young token behaves like an insider signal.
The net-flow term guards the failure mode of that thesis — KOLs crowded in
but actively distributing into the hype.

Missing data earns *half* credit for that sub-part — unknowns can't max a
token out, but they don't zero out an otherwise strong one either.
"""
from __future__ import annotations

from dataclasses import dataclass

from .config import config
from .models import Snapshot


@dataclass
class Component:
    label: str
    points: float
    max_points: float
    detail: str


@dataclass
class Scored:
    snap: Snapshot
    total: float
    components: list[Component]
    holder_growth_rate: float | None   # fraction/hour between scans, None = no baseline


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _ramp(x: float | None, lo: float, hi: float) -> float | None:
    """0 at lo, 1 at hi, linear between. None passes through."""
    if x is None:
        return None
    return _clamp01((x - lo) / (hi - lo))


def _award(fraction: float | None, max_pts: float) -> float:
    """Half credit when the underlying datum is unknown."""
    return max_pts * (0.5 if fraction is None else fraction)


# --- stage 1: hard gates ---------------------------------------------------

def gate(s: Snapshot) -> str | None:
    """Reason the token is rejected, or None if it passes."""
    age = s.age_hours
    if age is not None and age < config.min_token_age_hours:
        return f"too new ({age:.2f}h)"
    if age is not None and age > config.max_token_age_hours:
        return f"too old ({age:.1f}h)"
    if s.liquidity_usd is not None and s.liquidity_usd < config.min_liquidity_usd:
        return f"liquidity ${s.liquidity_usd:,.0f}"
    if s.mcap_usd is not None and not (config.min_mcap_usd <= s.mcap_usd <= config.max_mcap_usd):
        return f"mcap ${s.mcap_usd:,.0f} outside band"
    if s.holders is not None and s.holders < config.min_holders:
        return f"only {s.holders:.0f} holders"
    if s.top10_rate is not None and s.top10_rate > config.max_top10_rate:
        return f"top10 {s.top10_rate:.0%}"
    if s.honeypot:
        return "honeypot flag"
    if s.change_h1_pct is not None and s.change_h1_pct > config.max_h1_gain_pct:
        return f"already ran +{s.change_h1_pct:.0f}% (1h)"
    if s.txns_m5 is not None and s.txns_m5 < config.min_txns_m5:
        return f"dead flow ({s.txns_m5:.0f} txns/5m)"
    return None


# --- stage 2: components ---------------------------------------------------

def _momentum(s: Snapshot) -> Component:
    accel_pts = _award(_ramp(s.vol_accel, 0.8, 3.0), 12)
    ratio_pts = _award(_ramp(s.buy_ratio_m5, 0.50, 0.75), 8)
    accel = f"{s.vol_accel:.1f}×" if s.vol_accel is not None else "?"
    ratio = f"{s.buy_ratio_m5:.0%}" if s.buy_ratio_m5 is not None else "?"
    return Component("Momentum", accel_pts + ratio_pts, 20,
                     f"vol accel {accel}, buys {ratio} of 5m txns")


def _holder_growth(s: Snapshot, rate_per_hour: float | None) -> Component:
    base_pts = _award(_ramp(s.holders_per_hour, 20, 150), 7.5)
    delta_pts = _award(_ramp(rate_per_hour, 0.05, 0.30), 7.5)
    hph = f"{s.holders_per_hour:.0f}/h" if s.holders_per_hour is not None else "?"
    delta = f"{rate_per_hour:+.0%}/h" if rate_per_hour is not None else "first sighting"
    return Component("Holders", base_pts + delta_pts, 15, f"{hph} since launch, {delta}")


def _smart_money(s: Snapshot) -> Component:
    hint_pts = _award(_ramp(s.smart_hint, 1, 5), 8)
    # Net smart flow: are the smart wallets accumulating or distributing?
    nb, ns = s.smart_buys_h1, s.smart_sells_h1
    flow = None
    if nb is not None or ns is not None:
        nb, ns = nb or 0, ns or 0
        flow = nb / (nb + ns) if (nb + ns) > 0 else None
    flow_pts = _award(_ramp(flow, 0.5, 0.9), 5)
    flow_txt = f"{flow:.0%} of smart txns are buys" if flow is not None else "flow ?"
    if s.deep_checked:
        # Holding depth is the insider-crowding thesis — full marks needs a
        # genuine crowd (6+ wallets), not one lucky sniper.
        hold_pts = 15 * (_ramp(s.smart_holding, 2, 6) or 0)
        recent_pts = 7 * (_ramp(s.smart_recent_buys, 1, 3) or 0)
        detail = (f"{s.smart_holding} smart wallets holding, "
                  f"{s.smart_recent_buys} bought <2h, {flow_txt}")
    else:
        # Outside the deep-check budget this scan — neutral credit, not zero.
        hold_pts, recent_pts = 7.5, 3.5
        detail = f"{s.smart_hint:.0f} flagged on row (deep check skipped), {flow_txt}"
    return Component("Smart money", hint_pts + flow_pts + hold_pts + recent_pts, 35, detail)


def _safety(s: Snapshot) -> Component:
    top10_pts = _award(None if s.top10_rate is None else 1 - (_ramp(s.top10_rate, 0.15, 0.35) or 0), 6)
    insider_pts = _award(None if s.insider_rate is None else 1 - (_ramp(s.insider_rate, 0.05, 0.25) or 0), 4)
    rug_pts = _award(None if s.rug_score_norm is None else 1 - (_ramp(s.rug_score_norm, 10, 60) or 0), 6)
    renounce_pts = _award(None if s.mint_renounced is None else float(s.mint_renounced), 2)
    burn_pts = _award(None if s.burn_ratio is None else (_ramp(s.burn_ratio, 0.5, 0.95) or 0), 2)
    top10 = f"{s.top10_rate:.0%}" if s.top10_rate is not None else "?"
    rug = f"{s.rug_score_norm:.0f}" if s.rug_score_norm is not None else "?"
    return Component("Safety", top10_pts + insider_pts + rug_pts + renounce_pts + burn_pts, 20,
                     f"top10 {top10}, rugcheck risk {rug}/100")


def _timing(s: Snapshot) -> Component:
    """Full marks for coiling (flat-to-modest 1h move); fades as the run happens."""
    c = s.change_h1_pct
    if c is None:
        pts = 5.0
    elif -20 <= c <= 50:
        pts = 10.0
    elif c > 50:
        pts = 10 * (1 - _clamp01((c - 50) / 100))     # 0 by +150%
    else:
        pts = 10 * (1 - _clamp01((-20 - c) / 30))     # 0 by -50%
    move = f"{c:+.0f}% 1h" if c is not None else "1h move unknown"
    return Component("Timing", pts, 10, move)


def score(s: Snapshot, holder_growth_rate: float | None) -> Scored:
    components = [
        _momentum(s),
        _holder_growth(s, holder_growth_rate),
        _smart_money(s),
        _safety(s),
        _timing(s),
    ]
    return Scored(
        snap=s,
        total=sum(c.points for c in components),
        components=components,
        holder_growth_rate=holder_growth_rate,
    )
