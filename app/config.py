"""
Configuration loader for the Valera bot.  Reads environment variables
and provides strongly typed accessors.  If a variable is missing the bot
will fail fast on start.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

# Load variables from a local .env file if present
load_dotenv()


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


@dataclass
class Config:
    bot_token: str = field(default_factory=lambda: _require_env("BOT_TOKEN"))
    telegram_channel_id: str = field(default_factory=lambda: _require_env("TELEGRAM_CHANNEL_ID"))
    openai_api_key: str = field(default_factory=lambda: _require_env("OPENAI_API_KEY"))
    provider_token: str = field(default_factory=lambda: _require_env("PROVIDER_TOKEN"))
    admins: list[int] = field(default_factory=lambda: [int(i) for i in os.getenv("ADMINS", "").split(",") if i.strip().isdigit()])

    # Pricing table for available packages.  Key is slug used in invoices,
    # value is a tuple of (credits, amount_in_stars, description)
    pricing: dict[str, tuple[int, int, str]] = field(default_factory=lambda: {
        "pack_50": (50, 499, "Пакет на 50 генераций"),
        "pack_120": (120, 999, "Пакет на 120 генераций"),
        "pack_300": (300, 1999, "Пакет на 300 генераций"),
    })

    initial_credits: int = field(default_factory=lambda: 5)
    referral_bonus: int = field(default_factory=lambda: 10)
    currency: str = field(default_factory=lambda: "XTR")
