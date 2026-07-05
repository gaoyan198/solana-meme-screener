"""Paper-trading ledger over screener alerts.

Every Telegram alert opens a hypothetical fixed-notional position at the
alert price with a mechanical bracket: take profit at +PAPER_TP_PCT%, stop
loss at -PAPER_SL_PCT%, time exit after PAPER_HOLD_HOURS. The 5-min scan cron
tends the book: each open position is marked via Dexscreener and closed when
it crosses its bracket, rugs (pair gone / liquidity below exit-ability → $0),
or times out. Exits fill at the *sampled* price, so a spike that reverses
inside 5 minutes is missed — fine for judging the screen, generous vs real
slippage. SOL and BTC spot are recorded at entry/exit as benchmarks.
`report_text()` renders the running book for the daily Telegram digest.

The ledger lives in state.json next to the dedup/history data, so GitHub
Actions persists it between runs. Closing only happens in scan runs (which
commit state back); the report job is read-only.
"""
from __future__ import annotations

import time

from .benchmarks import sol_btc_prices
from .config import config
from .dexscreener import best_pair
from .geckoterminal import ath_price
from .log import get_logger
from .models import num
from .scoring import Scored
from .state import state

log = get_logger("paper")

# Below this the position is unsellable in practice — mark it to zero.
DEAD_LIQUIDITY_USD = 500
# Don't rug-close a position this young on a missing pair — Dexscreener may
# just be re-indexing; give it a couple of scans to reappear.
MIN_RUG_AGE_HOURS = 0.5


def targets(entry_price: float) -> tuple[float, float]:
    """(take_profit, stop_loss) prices for the mechanical bracket."""
    return (
        entry_price * (1 + config.paper_tp_pct / 100),
        entry_price * (1 - config.paper_sl_pct / 100),
    )


def record(sc: Scored) -> None:
    """Open a paper position for a fresh alert."""
    s = sc.snap
    if s.price_usd is None:
        log.warning("no entry price for %s — not paper-logging", s.symbol)
        return
    book = state.paper
    if s.address in book["open"] or any(p.get("mint") == s.address for p in book["closed"]):
        return
    sol, btc = sol_btc_prices()
    tp, sl = targets(s.price_usd)
    book["open"][s.address] = {
        "symbol": s.symbol,
        "score": round(sc.total, 1),
        "track": sc.track,
        "entry_ts": time.time(),
        "entry_price": s.price_usd,
        "entry_mcap": s.mcap_usd,
        "tp_price": tp,
        "sl_price": sl,
        "pair": s.pair_address,
        "sol_entry": sol,
        "btc_entry": btc,
    }
    log.info("paper open %s @ $%.6g (score %.0f, tp $%.6g, sl $%.6g)",
             s.symbol, s.price_usd, sc.total, tp, sl)


def backfill_alerted() -> None:
    """Adopt alerts sent before the ledger existed, at today's price."""
    book = state.paper
    known = set(book["open"]) | {p.get("mint") for p in book["closed"]}
    missing = [m for m in state.alerted if m not in known]
    if not missing:
        return
    sol, btc = sol_btc_prices()
    for mint in missing:
        pair = best_pair(mint)
        if not pair:
            continue
        price = num(pair, "priceUsd")
        if not price:
            continue
        symbol = ((pair.get("baseToken") or {}).get("symbol")) or mint[:6]
        tp, sl = targets(price)
        book["open"][mint] = {
            "symbol": str(symbol),
            "score": None,
            "entry_ts": time.time(),   # entry is backfill time, not alert time
            "entry_price": price,
            "entry_mcap": num(pair, "marketCap", "fdv"),
            "tp_price": tp,
            "sl_price": sl,
            "pair": pair.get("pairAddress"),
            "sol_entry": sol,
            "btc_entry": btc,
            "backfilled": True,
        }
        log.info("paper backfill %s @ $%.6g", symbol, price)


def tend() -> None:
    """Mark every open position; close on bracket cross, rug, or time expiry."""
    now = time.time()
    hold_sec = config.paper_hold_hours * 3600
    book = state.paper
    if not book["open"]:
        return
    to_close: list[tuple[str, str, float]] = []   # (mint, reason, exit price)
    for mint, pos in book["open"].items():
        if "tp_price" not in pos:   # positions opened before brackets existed
            pos["tp_price"], pos["sl_price"] = targets(pos["entry_price"])
        age_h = (now - pos["entry_ts"]) / 3600
        price = _mark(mint)
        if price > 0:
            # High-water mark, so the report can show the best X we saw.
            pos["peak_price"] = max(pos.get("peak_price", pos["entry_price"]), price)
        if price <= 0:
            if age_h >= MIN_RUG_AGE_HOURS:
                to_close.append((mint, "rug", 0.0))
        elif price >= pos["tp_price"]:
            to_close.append((mint, "tp", price))
        elif price <= pos["sl_price"]:
            to_close.append((mint, "sl", price))
        elif age_h * 3600 >= hold_sec:
            to_close.append((mint, "time", price))
    if not to_close:
        return
    sol, btc = sol_btc_prices()
    for mint, reason, price in to_close:
        pos = book["open"].pop(mint)
        pos["mint"] = mint
        pos["exit_ts"] = now
        pos["exit_price"] = price
        pos["exit_reason"] = reason
        pos["sol_exit"] = sol
        pos["btc_exit"] = btc
        book["closed"].append(pos)
        r = _ret(pos["entry_price"], price)
        log.info("paper close %s [%s]: %s", pos["symbol"], reason,
                 f"{r:+.0%}" if r is not None else "?")
    book["closed"] = book["closed"][-100:]


def _mark(mint: str) -> float:
    pair = best_pair(mint)
    if not pair:
        return 0.0
    liq = (pair.get("liquidity") or {}).get("usd") or 0
    if liq < DEAD_LIQUIDITY_USD:
        return 0.0
    return num(pair, "priceUsd") or 0.0


def _ret(entry: float | None, exit_: float | None) -> float | None:
    if not entry or exit_ is None:
        return None
    return (exit_ - entry) / entry


def _fmt_pos(mint: str, pos: dict, exit_price: float | None, sol_now: float | None,
             btc_now: float | None, closed: bool) -> tuple[str, float | None, float | None, float | None]:
    r = _ret(pos["entry_price"], exit_price)
    sol_r = _ret(pos.get("sol_entry"), pos.get("sol_exit") if closed else sol_now)
    btc_r = _ret(pos.get("btc_entry"), pos.get("btc_exit") if closed else btc_now)
    held_h = ((pos.get("exit_ts") or time.time()) - pos["entry_ts"]) / 3600
    tags = []
    if pos.get("track") == "momentum":
        tags.append("🔥MOM")
    if pos.get("exit_reason"):
        tags.append(pos["exit_reason"].upper())
    if pos.get("backfilled"):
        tags.append("backfilled")
    tag = f" [{', '.join(tags)}]" if tags else ""
    mult = 1 + r if r is not None else None
    entry_mcap = pos.get("entry_mcap")
    mcap_txt = _usd_short(entry_mcap)
    if entry_mcap and mult is not None:
        # Supply is ~constant for these tokens, so mcap scales with price.
        mcap_txt += f" → {_usd_short(entry_mcap * mult)}"
    # Peak since OUR entry only (pre-entry highs are noise for "what could we
    # have captured"): candle high filtered by entry_ts, merged with our own
    # 5-min marks. Includes post-exit highs — a stopped-out coin that later
    # mooned is an exit-rule failure worth seeing.
    pair_addr = pos.get("pair")
    if not pair_addr:
        pair_addr = (best_pair(mint) or {}).get("pairAddress")
    candle_high = ath_price(pair_addr, since_ts=pos["entry_ts"]) if pair_addr else None
    peak = max((p for p in (pos.get("peak_price"), candle_high) if p), default=None)
    peak_mult = peak / pos["entry_price"] if peak and pos["entry_price"] else None
    peak_txt = ""
    if peak_mult:
        peak_txt = f", peak {peak_mult:.2f}x since entry"
        if entry_mcap:
            mcap_txt += f" · peak {_usd_short(entry_mcap * peak_mult)}"
    # Symbol links to GMGN; the raw mint below is tap-to-copy in Telegram.
    line = (
        f"• [{pos['symbol']}](https://gmgn.ai/sol/token/{mint}) "
        f"*{f'{mult:.2f}x' if mult is not None else '?'}* "
        f"({f'{r:+.0%}' if r is not None else '?'}{peak_txt}) "
        f"({held_h:.0f}h){tag} · SOL {f'{sol_r:+.1%}' if sol_r is not None else '?'} "
        f"· BTC {f'{btc_r:+.1%}' if btc_r is not None else '?'}\n"
        f"  alerted @ {mcap_txt} mcap\n"
        f"  `{mint}`"
    )
    return line, r, sol_r, btc_r


def _usd_short(v: float | None) -> str:
    if v is None:
        return "?"
    # 9.95e5 threshold so 999.7k renders as $1.00M, not $1000k
    return f"${v / 1e6:.2f}M" if v >= 9.95e5 else f"${v / 1e3:.0f}k"


def report_text() -> str:
    book = state.paper
    if not book["open"] and not book["closed"]:
        return "📒 *Paper book* — no positions yet. Waiting for the first 70+ signal."

    sol_now, btc_now = sol_btc_prices()
    lines: list[str] = ["📒 *Paper book* — $%.0f per alert, %.0fh hold" %
                        (config.paper_notional_usd, config.paper_hold_hours), ""]
    rets: list[tuple[str, float | None, float | None, float | None]] = []

    if book["open"]:
        lines.append(f"*Open ({len(book['open'])})*")
        for mint, pos in book["open"].items():
            line, r, sol_r, btc_r = _fmt_pos(mint, pos, _mark(mint), sol_now, btc_now, closed=False)
            lines.append(line)
            rets.append((pos.get("track", "kol"), r, sol_r, btc_r))
        lines.append("")

    if book["closed"]:
        lines.append(f"*Closed ({len(book['closed'])})*")
        for pos in book["closed"]:
            line, r, sol_r, btc_r = _fmt_pos(pos.get("mint", "?"), pos, pos.get("exit_price"),
                                             sol_now, btc_now, closed=True)
            lines.append(line)
            rets.append((pos.get("track", "kol"), r, sol_r, btc_r))
        lines.append("")

    n = len(rets)
    notional = config.paper_notional_usd
    book_val = sum(notional * (1 + (r or 0)) for _, r, _, _ in rets)
    sol_val = sum(notional * (1 + (sr or 0)) for _, _, sr, _ in rets)
    btc_val = sum(notional * (1 + (br or 0)) for _, _, _, br in rets)
    staked = n * notional
    lines.append(
        f"*Book* ${book_val:,.0f} on ${staked:,.0f} staked "
        f"(*{book_val / staked:.2f}x, {(book_val - staked) / staked:+.1%}*)\n"
        f"Same $ in SOL ${sol_val:,.0f} ({(sol_val - staked) / staked:+.1%}) · "
        f"BTC ${btc_val:,.0f} ({(btc_val - staked) / staked:+.1%})"
    )
    # Per-track split, once the momentum track has positions to compare.
    mom = [t for t in rets if t[0] == "momentum"]
    if mom:
        kol = [t for t in rets if t[0] != "momentum"]
        for label, grp in (("🎯 KOL", kol), ("🔥 Momentum", mom)):
            if not grp:
                continue
            g_staked = len(grp) * notional
            g_val = sum(notional * (1 + (r or 0)) for _, r, _, _ in grp)
            lines.append(f"{label}: {len(grp)} pos · ${g_val:,.0f} on ${g_staked:,.0f} "
                         f"({g_val / g_staked:.2f}x)")
    return "\n".join(lines)
