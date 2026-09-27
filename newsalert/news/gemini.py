"""Gemini classifier for BusinessLine headlines (REST, via an injected httpx client).

Free tier (the owner's choice, recorded in the README): Google may use submitted content
to improve its products. Only headline + RSS summary are sent - never article text - and
never NSE content (see rules.py).

Limits: Google no longer publishes free-tier numbers; they are per project and shown in
AI Studio. We stay inside conservative configured caps (requests/minute, requests/day,
counted per Pacific-time day because that is when Google resets daily quotas), batch
several headlines per request, and on HTTP 429 pause for Google's retryDelay and record
the quota it reports.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable
from zoneinfo import ZoneInfo

import httpx
from pydantic import ValidationError

from ..ratelimit import RateLimiter
from .models import BatchResponse, Classification, gemini_response_schema, restrict_to_universe

log = logging.getLogger(__name__)
PACIFIC = ZoneInfo("America/Los_Angeles")

SYSTEM = """You classify Indian business news headlines for an equity monitor covering the Nifty 500.
For each item return:
- event_type: exactly one of the allowed values.
- affected: the listed NSE stocks the news is likely to move. Use ONLY symbols from the provided
  list, exactly as written. relation is "direct" for the company the news is about, otherwise
  competitor, supplier, customer or sector peer. direction is the expected short-term price
  direction; strength low/medium/high; reason is one short line (under 25 words).
  Return an empty list if no listed stock is materially affected.
- confidence: 0 to 1, your confidence in the classification as a whole.
Base the answer only on the headline and summary given. Do not invent facts."""


class GeminiError(Exception):
    pass


class QuotaExhausted(GeminiError):
    def __init__(self, retry_after: float, detail: dict):
        super().__init__(f"quota exhausted, retry after {retry_after:.0f}s")
        self.retry_after, self.detail = retry_after, detail


@dataclass
class ItemIn:
    id: int
    headline: str
    summary: str | None


@dataclass
class ItemOut:
    id: int
    classification: Classification | None     # None -> unclassified
    rejected_tickers: list[str]
    error: str | None = None


def _retry_delay(body: dict, default: float = 60.0) -> tuple[float, dict]:
    """Parse google.rpc.RetryInfo / QuotaFailure details from a 429 body."""
    delay, quota = default, {}
    for d in (body.get("error") or {}).get("details") or []:
        t = d.get("@type", "")
        if t.endswith("RetryInfo") and d.get("retryDelay"):
            m = re.match(r"([\d.]+)s", str(d["retryDelay"]))
            if m:
                delay = float(m.group(1))
        if t.endswith("QuotaFailure"):
            for v in d.get("violations") or []:
                quota = {k: v.get(k) for k in ("quotaMetric", "quotaId", "quotaValue") if v.get(k)}
    return delay, quota


class GeminiClassifier:
    def __init__(self, http: httpx.AsyncClient, api_key: str, *, model: str, tickers: dict[str, str],
                 store, rpm: int, rpd: int, batch_size: int = 10,
                 base_url: str = "https://generativelanguage.googleapis.com/v1beta",
                 clock: Callable[[], float] = time.time, limiter: RateLimiter | None = None):
        self.http, self._key, self.model, self.store = http, api_key, model, store
        self.tickers = tickers                      # symbol -> company name
        self.universe = set(tickers)
        self.rpd, self.batch_size, self.base_url, self.clock = rpd, batch_size, base_url, clock
        self.limiter = limiter or RateLimiter([(rpm, 60.0)])
        self.paused_until = 0.0
        self.last_quota: dict = {}
        self._ticker_block = "\n".join(f"{s}: {n}" for s, n in sorted(tickers.items()))

    # -- quota ----------------------------------------------------------------------------
    def _day(self) -> str:
        return datetime.fromtimestamp(self.clock(), PACIFIC).date().isoformat()

    def used_today(self) -> int:
        return self.store.usage("gemini", self._day())

    def can_request(self) -> bool:
        return self.clock() >= self.paused_until and self.used_today() < self.rpd

    # -- one request --------------------------------------------------------------------------
    def _prompt(self, items: list[ItemIn]) -> str:
        lines = [f"Allowed NSE symbols (symbol: company):\n{self._ticker_block}\n", "Items:"]
        for it in items:
            lines.append(json.dumps({"id": it.id, "headline": it.headline, "summary": it.summary or ""},
                                    ensure_ascii=False))
        return "\n".join(lines)

    async def _call(self, items: list[ItemIn]) -> str:
        if not self.can_request():
            raise QuotaExhausted(max(1.0, self.paused_until - self.clock()), self.last_quota)
        await self.limiter.acquire()
        self.store.add_usage("gemini", self._day())
        body = {
            "systemInstruction": {"parts": [{"text": SYSTEM}]},
            "contents": [{"role": "user", "parts": [{"text": self._prompt(items)}]}],
            "generationConfig": {"responseMimeType": "application/json",
                                 "responseSchema": gemini_response_schema(), "temperature": 0},
        }
        try:
            resp = await self.http.post(f"{self.base_url}/models/{self.model}:generateContent",
                                        json=body, headers={"x-goog-api-key": self._key})
        except httpx.HTTPError as e:
            raise GeminiError(f"request failed: {type(e).__name__}") from None
        if resp.status_code == 429:
            try:
                detail = resp.json()
            except ValueError:
                detail = {}
            delay, quota = _retry_delay(detail)
            self.paused_until = self.clock() + delay
            self.last_quota = quota
            raise QuotaExhausted(delay, quota)
        if resp.status_code != 200:
            msg = ""
            try:
                msg = (resp.json().get("error") or {}).get("status", "")
            except ValueError:
                pass
            raise GeminiError(f"HTTP {resp.status_code} {msg}".strip())
        try:
            data = resp.json()
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except (ValueError, KeyError, IndexError, TypeError):
            return ""   # treated as invalid JSON by the caller

    def _parse(self, text: str, expected: set[int]) -> dict[int, Classification]:
        """Valid classifications by id; anything invalid is simply absent."""
        try:
            batch = BatchResponse.model_validate_json(text)
        except (ValidationError, ValueError):
            # salvage per-item: each item validated independently
            try:
                raw = json.loads(text)
                raw_items = raw.get("items", []) if isinstance(raw, dict) else []
            except (ValueError, AttributeError):
                return {}
            out = {}
            for r in raw_items:
                try:
                    bi = BatchResponse.model_validate({"items": [r]}).items[0]
                except (ValidationError, ValueError):
                    continue
                if bi.id in expected:
                    out[bi.id] = Classification(event_type=bi.event_type, affected=bi.affected,
                                                confidence=bi.confidence)
            return out
        return {bi.id: Classification(event_type=bi.event_type, affected=bi.affected, confidence=bi.confidence)
                for bi in batch.items if bi.id in expected}

    # -- public ---------------------------------------------------------------------------------
    async def classify(self, items: list[ItemIn]) -> list[ItemOut]:
        """Classify a batch. Items whose JSON is invalid are retried once on their own; if that
        fails too they come back with classification=None (store as unclassified).
        Raises QuotaExhausted before anything is decided if the quota is hit on the first call."""
        results: dict[int, ItemOut] = {}
        text = await self._call(items)
        got = self._parse(text, {i.id for i in items})
        retry = [i for i in items if i.id not in got]
        for it in retry:
            try:
                single = self._parse(await self._call([it]), {it.id})
            except QuotaExhausted:
                break   # leave the rest pending; they will be retried when quota returns
            if it.id in single:
                got[it.id] = single[it.id]
            else:
                results[it.id] = ItemOut(it.id, None, [], "invalid JSON twice")
        for it in items:
            if it.id in got:
                clean, rejected = restrict_to_universe(got[it.id], self.universe)
                results[it.id] = ItemOut(it.id, clean, rejected)
        return [results[i.id] for i in items if i.id in results]
