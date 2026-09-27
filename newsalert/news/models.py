"""Schemas for news classification. Gemini output is validated against these; anything
that doesn't validate is retried once and then stored as unclassified."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class EventType(str, Enum):
    results = "results"
    guidance = "guidance"
    merger_acquisition = "merger/acquisition"
    order_win = "order/contract win"
    rating_change = "rating change"
    regulatory_action = "regulatory action"
    fraud_legal = "fraud/legal"
    management_change = "management change"
    capital_raise = "capital raise"
    dividend_buyback = "dividend/buyback"
    other = "other"


class Relation(str, Enum):
    direct = "direct"
    competitor = "competitor"
    supplier = "supplier"
    customer = "customer"
    sector_peer = "sector peer"


class Direction(str, Enum):
    up = "up"
    down = "down"


class Strength(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"


class AffectedStock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticker: str = Field(min_length=1, max_length=20)
    relation: Relation
    direction: Direction
    strength: Strength
    reason: str = Field(min_length=1, max_length=240)

    @field_validator("ticker")
    @classmethod
    def _norm(cls, v: str) -> str:
        v = v.strip().upper()
        for suffix in (".NS", ".NSE", "-EQ", ".BO"):
            if v.endswith(suffix):
                v = v[: -len(suffix)]
        return v


class Classification(BaseModel):
    """What Gemini must return for one news item."""
    model_config = ConfigDict(extra="forbid")
    event_type: EventType
    affected: list[AffectedStock] = Field(default_factory=list, max_length=10)
    confidence: float = Field(ge=0.0, le=1.0)


class BatchItem(Classification):
    id: int


class BatchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[BatchItem]


def restrict_to_universe(c: Classification, tickers: set[str]) -> tuple[Classification, list[str]]:
    """Drop affected stocks whose ticker isn't in tickers.csv. Returns (cleaned, rejected)."""
    keep, rejected, seen = [], [], set()
    for s in c.affected:
        if s.ticker not in tickers:
            rejected.append(s.ticker)
        elif (s.ticker, s.relation) not in seen:
            seen.add((s.ticker, s.relation))
            keep.append(s)
    return c.model_copy(update={"affected": keep}), rejected


def gemini_response_schema() -> dict:
    """OpenAPI-style schema for Gemini's structured output (responseSchema)."""
    stock = {
        "type": "OBJECT",
        "properties": {
            "ticker": {"type": "STRING"},
            "relation": {"type": "STRING", "enum": [r.value for r in Relation]},
            "direction": {"type": "STRING", "enum": [d.value for d in Direction]},
            "strength": {"type": "STRING", "enum": [s.value for s in Strength]},
            "reason": {"type": "STRING"},
        },
        "required": ["ticker", "relation", "direction", "strength", "reason"],
    }
    item = {
        "type": "OBJECT",
        "properties": {
            "id": {"type": "INTEGER"},
            "event_type": {"type": "STRING", "enum": [e.value for e in EventType]},
            "affected": {"type": "ARRAY", "items": stock},
            "confidence": {"type": "NUMBER"},
        },
        "required": ["id", "event_type", "affected", "confidence"],
    }
    return {"type": "OBJECT", "properties": {"items": {"type": "ARRAY", "items": item}}, "required": ["items"]}
