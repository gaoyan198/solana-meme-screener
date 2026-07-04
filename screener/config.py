"""Configuration + filter thresholds, loaded from the environment / .env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _float(key: str, default: float) -> float:
    raw = os.getenv(key, "").strip()
    return float(raw) if raw else default


def _int(key: str, default: int) -> int:
    raw = os.getenv(key, "").strip()
    return int(raw) if raw else default


@dataclass
class Config:
    # --- alerts ---
    telegram_bot_token: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", "").strip())
    telegram_chat_id: str = field(default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID", "").strip())

    # --- data sources ---
    gmgn_base: str = field(default_factory=lambda: os.getenv("GMGN_BASE", "https://gmgn.ai").strip().rstrip("/"))
    gmgn_proxy: str = field(default_factory=lambda: os.getenv("GMGN_PROXY", "").strip())
    gmgn_api_key: str = field(default_factory=lambda: os.getenv("GMGN_API_KEY", "").strip())

    # --- hard gates (any fail = token skipped, no score) ---
    min_token_age_hours: float = field(default_factory=lambda: _float("MIN_TOKEN_AGE_HOURS", 0.25))
    max_token_age_hours: float = field(default_factory=lambda: _float("MAX_TOKEN_AGE_HOURS", 24))
    min_liquidity_usd: float = field(default_factory=lambda: _float("MIN_LIQUIDITY_USD", 10_000))
    min_mcap_usd: float = field(default_factory=lambda: _float("MIN_MCAP_USD", 15_000))
    max_mcap_usd: float = field(default_factory=lambda: _float("MAX_MCAP_USD", 1_500_000))
    min_holders: int = field(default_factory=lambda: _int("MIN_HOLDERS", 60))
    max_top10_rate: float = field(default_factory=lambda: _float("MAX_TOP10_RATE", 0.35))
    # "Too late" gate: if it already did this in the last hour, the run happened.
    max_h1_gain_pct: float = field(default_factory=lambda: _float("MAX_H1_GAIN_PCT", 200))
    min_txns_m5: int = field(default_factory=lambda: _int("MIN_TXNS_M5", 10))

    # --- scoring / alerting ---
    min_score: float = field(default_factory=lambda: _float("MIN_SCORE", 70))
    max_alerts_per_scan: int = field(default_factory=lambda: _int("MAX_ALERTS_PER_SCAN", 3))
    alert_cooldown_hours: float = field(default_factory=lambda: _float("ALERT_COOLDOWN_HOURS", 24))

    # --- scan budget (stay under GMGN's rate limit on a 5-min cron) ---
    trending_limit: int = field(default_factory=lambda: _int("TRENDING_LIMIT", 100))
    max_enrich: int = field(default_factory=lambda: _int("MAX_ENRICH", 20))
    max_deep_checks: int = field(default_factory=lambda: _int("MAX_DEEP_CHECKS", 10))
    request_delay_sec: float = field(default_factory=lambda: _float("REQUEST_DELAY_SEC", 0.6))

    # --- which GMGN wallet cohorts count as "smart money" ---
    smart_tags: list[str] = field(
        default_factory=lambda: [
            t.strip() for t in os.getenv("SMART_TAGS", "smart_degen,renowned").split(",") if t.strip()
        ]
    )

    def validate(self) -> None:
        problems = []
        if not (self.telegram_bot_token and self.telegram_chat_id):
            problems.append(
                "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — alerts would go nowhere."
            )
        if self.min_mcap_usd >= self.max_mcap_usd:
            problems.append("MIN_MCAP_USD must be below MAX_MCAP_USD.")
        if problems:
            raise SystemExit("Config error(s):\n  - " + "\n  - ".join(problems))


config = Config()
