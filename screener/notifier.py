"""Telegram alerts (fire-and-forget; never raises into the scan loop)."""
from __future__ import annotations

import requests

from . import paper
from .config import config
from .log import get_logger
from .scoring import Scored

log = get_logger("notify")


def _bar(points: float, max_points: float, width: int = 5) -> str:
    filled = round(width * points / max_points) if max_points else 0
    return "▰" * filled + "▱" * (width - filled)


def _fmt(sc: Scored) -> str:
    s = sc.snap
    age = f"{s.age_hours:.1f}h" if s.age_hours is not None else "?"
    mcap = f"${s.mcap_usd:,.0f}" if s.mcap_usd is not None else "?"
    liq = f"${s.liquidity_usd:,.0f}" if s.liquidity_usd is not None else "?"
    holders = f"{s.holders:.0f}" if s.holders is not None else "?"
    lines = [
        f"🎯 *Screener hit* — `{s.symbol}` · *{sc.total:.0f}/100*",
        f"{s.name}",
        "",
    ]
    for c in sc.components:
        lines.append(f"`{_bar(c.points, c.max_points)}` *{c.label}* {c.points:.0f}/{c.max_points:.0f} — {c.detail}")
    lines += [
        "",
        f"• MCAP *{mcap}* · Liq *{liq}* · Age *{age}* · Holders *{holders}*",
    ]
    if s.price_usd:
        tp, sl = paper.targets(s.price_usd)
        lines += [
            "",
            "📐 *Plan (mechanical, not advice)*",
            f"Entry ≤ *${s.price_usd * 1.1:.6g}* (alert ${s.price_usd:.6g} — don't chase past +10%)",
            f"TP *${tp:.6g}* (+{config.paper_tp_pct:.0f}%) · Stop *${sl:.6g}* "
            f"(−{config.paper_sl_pct:.0f}%) · Time exit {config.paper_hold_hours:.0f}h",
        ]
    lines += [
        "",
        f"`{s.address}`",
        f"[GMGN](https://gmgn.ai/sol/token/{s.address}) · "
        f"[DexScreener](https://dexscreener.com/solana/{s.address}) · "
        f"[RugCheck](https://rugcheck.xyz/tokens/{s.address})",
    ]
    return "\n".join(lines)


def send_hit(sc: Scored) -> bool:
    return _send(_fmt(sc))


def send_text(text: str) -> bool:
    return _send(text)


def _send(text: str) -> bool:
    """True only if Telegram confirmed delivery — callers use this to retry.

    Telegram rejects messages with HTTP 400/403 (chat not found until the user
    /starts the bot, Markdown entity errors, blocked bot); those must not be
    swallowed as success. Markdown parse errors are retried as plain text.
    """
    if not (config.telegram_bot_token and config.telegram_chat_id):
        log.info("(telegram not configured) %s", text.replace("\n", " ")[:160])
        return False
    ok, desc = _post(text, markdown=True)
    if ok:
        return True
    if "parse" in desc.lower() or "entit" in desc.lower():
        log.warning("Markdown rejected (%s) — resending plain", desc)
        ok, desc = _post(text, markdown=False)
        if ok:
            return True
    log.error("Telegram rejected message: %s", desc)
    return False


def _post(text: str, markdown: bool) -> tuple[bool, str]:
    payload = {
        "chat_id": config.telegram_chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if markdown:
        payload["parse_mode"] = "Markdown"
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{config.telegram_bot_token}/sendMessage",
            json=payload,
            timeout=10,
        )
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code == 200 and body.get("ok"):
            return True, ""
        return False, str(body.get("description") or f"HTTP {r.status_code}")
    except Exception as e:  # noqa: BLE001
        return False, str(e)
