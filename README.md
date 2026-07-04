# solana-meme-screener

Alert-only Solana memecoin screener modelled on [stalk.fun](https://stalk.fun)'s
"Print Scan": surface young tokens **before** the run, not after. No trading,
no keys, no wallet — it only sends Telegram messages.

## The deciphered algorithm

stalk.fun doesn't publish its method, but its own marketing and guides say the
Print Scan analyses **volume, holders, KOL activity and price variation** on
new launches, and tells users to hand-check top-holder distribution and
bundle-controlled wallets. This repo turns that into an explicit two-stage
screen:

**Stage 1 — hard gates** (binary, any fail = dropped): age 15min–24h,
liquidity ≥ $10k, mcap $15k–$1.5M, ≥ 60 holders, top-10 holders ≤ 35%, no
honeypot flag, not already up >200% in the last hour, ≥ 10 txns in the last
5 minutes.

**Stage 2 — composite score, 0–100** (alert at ≥ 70):

| Component | Max | What it measures |
|---|---|---|
| Momentum | 25 | 5-min volume run-rate vs 1h baseline (accel ≥ 3× = full marks); buy share of 5-min txns |
| Holder growth | 20 | holders/hour since launch; holder growth between scans (state file) |
| Smart money | 25 | GMGN `smart_degen` + `renowned` wallets holding; how many bought < 2h ago |
| Safety | 20 | top-10 %, insider ("rat trader") %, RugCheck risk score, mint renounced, LP burn |
| Entry timing | 10 | full marks for "coiling" (−20%…+50% on 1h); fades to 0 by +150% (you missed it) or −50% (knife) |

Missing data earns *half* credit for that sub-part — unknowns can't max a
token out, but can't zero out a strong one either.

Data: GMGN web endpoints (via `curl_cffi` Cloudflare bypass, same client as
solana-kol-bot), Dexscreener public API (free) for microstructure, RugCheck
public API (free) for a second safety opinion.

## Run

```bash
pip install -r requirements.txt
cp .env.example .env       # fill in Telegram token + chat id
python main.py table       # full ranked table, no alerts — use this to calibrate
python main.py             # one scan cycle, alerts new hits ≥ MIN_SCORE
python main.py report      # send the paper-trading book to Telegram
python main.py dump        # raw GMGN JSON, for when they rename fields
python main.py test-alert  # check Telegram wiring
```

## Paper trading

Every alert carries a mechanical trade plan and automatically opens a
hypothetical **$100** position at the alert price, with SOL and BTC spot
recorded as benchmarks (CoinGecko). The 5-min scan cron tends the book — each
position closes on whichever comes first:

- **TP** +100% · **Stop** −40% (filled at the sampled 5-min price, so a spike
  that reverses within one scan is missed; still generous vs real slippage)
- **Rug**: pair gone or liquidity < $500 → marked to $0, because that's what
  an unsellable position is worth
- **Time exit** after 72h

A daily report (09:00 SGT) shows every flagged coin, its return, exit reason,
and what the same $100 in SOL or BTC would have done. This is the built-in
reality check: if the book doesn't beat SOL after a few weeks of alerts, the
screen has no edge. The plan levels are position-management arithmetic, not a
price prediction — nobody can honestly derive a target for a 3-hour-old token.

## Deploy (GitHub Actions, every 5 min)

1. Create a **private** GitHub repo, push this folder.
2. Settings → Secrets: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
   (optional: `GMGN_PROXY` if Actions IPs get Cloudflare-blocked).
3. Settings → Variables (optional): any threshold from `.env.example`.
4. The workflow commits `state.json` back after each run — that file is the
   dedup memory *and* the holder-growth baseline between scans.

## Honest expectations

- stalk.fun's "75–90% daily win rate" is marketing. Nobody publishes audited
  results on this. Assume most alerts still go to zero — the base rate for
  young memecoins is a rug.
- This screener sees what smart-money trackers see, ~5 minutes late (cron
  granularity). Sniping-speed entries are not the point; filtering garbage is.
- The score is a heuristic, not a backtest. Run `python main.py table` for a
  week and check what scored 70+ actually did before wiring real money to it.
- Sized for entertainment: a fixed small bucket, never topped up from core
  savings, never margin.
