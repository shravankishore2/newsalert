"""Load config.yaml and secrets from .env."""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


@dataclass(frozen=True)
class Secrets:
    finnhub_api_key: str
    newsapi_api_key: str
    telegram_bot_token: str
    telegram_chat_id: str

    def missing(self, *names: str) -> list[str]:
        """Env-var names (e.g. FINNHUB_API_KEY) of the given fields that are empty."""
        return [n.upper() for n in names if not getattr(self, n)]


@dataclass(frozen=True)
class Ticker:
    symbol: str
    name: str


def load_config(path: str | Path = "config.yaml") -> dict[str, Any]:
    with open(path) as f:
        return yaml.safe_load(f)


def load_secrets(env_file: str | Path = ".env") -> Secrets:
    load_dotenv(env_file)
    return Secrets(
        finnhub_api_key=os.getenv("FINNHUB_API_KEY", ""),
        newsapi_api_key=os.getenv("NEWSAPI_API_KEY", ""),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
    )


def load_tickers(path: str | Path) -> list[Ticker]:
    with open(path, newline="") as f:
        return [Ticker(r["symbol"].strip(), r.get("name", "").strip()) for r in csv.DictReader(f)]
