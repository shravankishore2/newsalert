"""Rule-based classifier for NSE corporate announcements.

NSE's terms forbid transmitting or storing its content without written permission, so
NSE announcements are never sent to an LLM and their text is never stored. The filing
subject is mapped to an event type here, in memory; only the derived fields are kept.
The only affected stock is the announcing company (relation "direct"). A direction is
given only where the event type implies one; otherwise it is left unknown (None).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .models import EventType

# (event type, direction or None, strength, pattern) - first match wins, so order matters.
RULES: list[tuple[EventType, str | None, str, str]] = [
    (EventType.other, None, "low", r"trading window|closure of trading|investor meet|analyst|con\.? ?call|"
                                    r"newspaper publication|loss of share cert|duplicate share|"
                                    r"shareholders? meeting|annual general meeting|\bagm\b|postal ballot|"
                                    r"record date|book closure|compliance certificate|reg(?:ulation)?\.? ?74|"
                                    r"investor presentation|press release|updates?$"),
    (EventType.fraud_legal, "down", "medium", r"fraud|default|insolvency|\bcirp\b|nclt|forensic|"
                                              r"search and seizure|raid|arrest|litigation|dispute"),
    (EventType.regulatory_action, "down", "low", r"action\(?s\)? taken or orders? passed|penalt|show cause|"
                                                 r"sebi order|demand order|tax demand|gst demand|suspension"),
    (EventType.order_win, "up", "medium", r"bagging|receiving of orders|order (?:win|received|inflow)|"
                                          r"letter of (?:award|intent)|contract (?:award|win)|work order"),
    (EventType.rating_change, "up", "low", r"credit rating.*upgrad|upgrad.*credit rating"),
    (EventType.rating_change, "down", "low", r"credit rating.*downgrad|downgrad.*credit rating"),
    (EventType.rating_change, None, "low", r"credit rating"),
    (EventType.dividend_buyback, "up", "low", r"buy-?back"),
    (EventType.dividend_buyback, "up", "low", r"dividend"),
    (EventType.merger_acquisition, None, "medium", r"acquisition|amalgamation|merger|scheme of arrangement|"
                                                   r"demerger|takeover|open offer"),
    (EventType.capital_raise, None, "low", r"fund ?raising|qualified institution|\bqip\b|rights issue|"
                                           r"preferential (?:issue|allotment)|allotment of|"
                                           r"issue of (?:equity|shares|warrants|ncd|debentures)"),
    (EventType.management_change, None, "low", r"resignation|appointment|change in (?:directors|management|kmp)|"
                                               r"cessation|key managerial"),
    (EventType.guidance, None, "low", r"guidance|outlook"),
    (EventType.results, None, "medium", r"financial results?|outcome of board meeting|quarterly results|"
                                        r"audited results|unaudited results"),
]
_COMPILED = [(e, d, s, re.compile(p, re.I)) for e, d, s, p in RULES]

_SUBJECT = re.compile(r"has informed the Exchange (?:about|regarding|that)?\s*(.*)", re.I | re.S)


@dataclass(frozen=True)
class RuleResult:
    event_type: EventType
    direction: str | None
    strength: str
    label: str          # short derived label shown in the UI instead of NSE's text


LABELS = {
    EventType.results: "Financial results", EventType.guidance: "Guidance/outlook",
    EventType.merger_acquisition: "Merger/acquisition filing", EventType.order_win: "Order/contract win",
    EventType.rating_change: "Credit rating update", EventType.regulatory_action: "Regulatory action/order",
    EventType.fraud_legal: "Fraud/default/legal filing", EventType.management_change: "Management change",
    EventType.capital_raise: "Capital raise/allotment", EventType.dividend_buyback: "Dividend/buyback",
    EventType.other: "Other filing",
}


def classify_nse(description: str) -> RuleResult:
    """Map an NSE announcement description to an event type. The text is not retained."""
    m = _SUBJECT.search(description or "")
    subject = (m.group(1) if m else description or "").strip()
    for event, direction, strength, pat in _COMPILED:
        if pat.search(subject):
            label = LABELS[event]
            if event is EventType.rating_change and direction:
                label = f"Credit rating {'upgrade' if direction == 'up' else 'downgrade'}"
            return RuleResult(event, direction, strength, label)
    return RuleResult(EventType.other, None, "low", LABELS[EventType.other])
