"""Entry point.

Usage:
  python main.py            # one scan cycle: score candidates, alert new hits
  python main.py table      # run the pipeline, print the full ranked table, no alerts
  python main.py dump [MINT]# print raw GMGN JSON to calibrate field mappings
  python main.py test-alert # send a dummy Telegram message to check wiring
"""
from __future__ import annotations

import json
import sys

from screener.config import config
from screener.gmgn import client
from screener.log import get_logger
from screener import notifier
from screener.scanner import scan
from screener.state import state

log = get_logger("main")


def run_scan(alert: bool = True) -> None:
    if alert:
        config.validate()
    hits = scan()
    new = 0
    for sc in hits:
        if not alert or state.recently_alerted(sc.snap.address):
            continue
        if new >= config.max_alerts_per_scan:
            log.info("Alert cap reached; %s (%.0f) suppressed", sc.snap.symbol, sc.total)
            continue
        notifier.send_hit(sc)
        state.mark_alerted(sc.snap.address)
        new += 1
    state.save()
    log.info("Scan complete: %d hit(s) ≥ %.0f, %d new alert(s) sent.",
             len(hits), config.min_score, new)


def dump(argv: list[str]) -> None:
    """Fetch raw JSON so you can see GMGN's current field names."""
    tokens = client.trending(period="1h", orderby="swaps", limit=5)
    print("=== trending[0] ===")
    print(json.dumps(tokens[0] if tokens else {}, indent=2)[:4000])
    if tokens:
        addr = tokens[0].get("address") or tokens[0].get("token_address")
        if len(argv) > 1:  # allow: dump <mint>
            addr = argv[1]
        for tag in config.smart_tags:
            traders = client.traders_by_tag(addr, tag)
            print(f"\n=== traders_by_tag({addr}, {tag}) — {len(traders)} traders, first 2 ===")
            print(json.dumps(traders[:2], indent=2)[:3500])


def main() -> None:
    argv = sys.argv[1:]
    if argv and argv[0] == "dump":
        dump(argv)
    elif argv and argv[0] == "test-alert":
        notifier.send_text("✅ solana-meme-screener wired up correctly.")
    elif argv and argv[0] == "table":
        run_scan(alert=False)
    else:
        run_scan()


if __name__ == "__main__":
    main()
