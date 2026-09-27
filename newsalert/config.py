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
    dhan_client_id: str
    dhan_pin: str
    dhan_totp_secret: str
    dhan_access_token: str
    dashboard_password: str
    gemini_api_key: str

    def missing(self, *names: str) -> list[str]:
        """Env-var names (e.g. DHAN_PIN) of the given fields that are empty."""
        return [n.upper() for n in names if not getattr(self, n)]

    def __repr__(self) -> str:  # never print secret values
        return "Secrets(" + ", ".join(f"{k}={'set' if v else 'unset'}" for k, v in vars(self).items()) + ")"


@dataclass(frozen=True)
class Ticker:
    symbol: str
    name: str
    security_id: str


def load_config(path: str | Path = "config.yaml") -> dict[str, Any]:
    with open(path) as f:
        return yaml.safe_load(f)


def load_secrets(env_file: str | Path = ".env") -> Secrets:
    load_dotenv(env_file)
    return Secrets(
        dhan_client_id=os.getenv("DHAN_CLIENT_ID", ""),
        dhan_pin=os.getenv("DHAN_PIN", ""),
        dhan_totp_secret=os.getenv("DHAN_TOTP_SECRET", ""),
        dhan_access_token=os.getenv("DHAN_ACCESS_TOKEN", ""),
        dashboard_password=os.getenv("DASHBOARD_PASSWORD", ""),
        gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
    )


def load_tickers(path: str | Path) -> list[Ticker]:
    with open(path, newline="") as f:
        return [Ticker(r["symbol"].strip(), r.get("name", "").strip(), r["security_id"].strip())
                for r in csv.DictReader(f)]
