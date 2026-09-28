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
# Refined 2026-09-28 against that day's feed (subjects analysed in memory, not stored):
# routine filings first; press-release titles are classified by their content, not skipped.
_ROUTINE = (r"trading window|closure of trading|schedule of (?:analyst|investor|meet)|"
            r"(?:outcome|update) (?:of|on) (?:analyst|investor|participation)|investors? ?/ ?analysts? (?:interaction|meet)|"
            r"analyst ?/ ?institutional investor|investor meet|con\.? ?call|earnings call|"
            r"newspaper publication|loss of share cert|duplicate share|physical securities|"
            r"proceedings? of (?:the )?(?:#?\d*\w* )?(?:annual )?general meeting|annual general meeting|\bagm\b|"
            r"postal ballot|voting results|scrutini[sz]er|compliance certificate|reg(?:ulation)?\.? ?74|"
            r"commercial paper|interest ?/ ?principal|\bncds?\b|debentures? (?:interest|redemption)|"
            r"exercise of .*options|\besop\b|employee stock option|"
            r"corporate guarantee|investor presentation|^general updates?$|^updates?$|^outcome$")
RULES: list[tuple[EventType, str | None, str, str]] = [
    # corporate actions before "routine": a dividend/bonus/split/buyback record date is an action
    (EventType.dividend_buyback, "up", "medium", r"buy-?back"),
    (EventType.dividend_buyback, "up", "low", r"bonus (?:issue|shares?)|issue of bonus"),
    (EventType.dividend_buyback, "up", "low", r"(?:stock )?split|sub-?division of (?:equity )?shares"),
    (EventType.dividend_buyback, "up", "low", r"(?:interim|final|special)? ?dividend(?! distribution policy)"),
    (EventType.other, None, "low", _ROUTINE),
    (EventType.fraud_legal, "down", "medium", r"fraud|default(?:ed)? in (?:payment|repayment)|insolvency|\bcirp\b|"
                                              r"nclt|forensic|search and seizure|raid|arrest"),
    (EventType.fraud_legal, None, "low", r"litigation|dispute|legal proceeding"),
    (EventType.regulatory_action, "down", "low", r"action\(?s\)? (?:taken|initiated)|orders? passed|penalt|fine (?:levied|imposed)|"
                                                 r"show cause|sebi order|demand order|tax demand|gst demand|suspension"),
    (EventType.order_win, "up", "medium", r"bagging|receiving of orders|order (?:win|received|inflow|book)|bags? |"
                                          r"secures? (?:an? )?(?:order|contract)|wins? (?:an? )?(?:order|contract)|"
                                          r"letter of (?:award|intent)|contract (?:award|win)|work order|"
                                          r"signs? (?:an? )?(?:contract|agreement worth)|contracts? (?:to|for|worth)"),
    (EventType.rating_change, "up", "low", r"credit rating.*(?:upgrad|revised (?:upward|to positive)|outlook .*positive|watch .*positive)|"
                                           r"upgrad.*credit rating"),
    (EventType.rating_change, "down", "low", r"credit rating.*(?:downgrad|revised (?:downward|to negative)|outlook .*negative|watch .*negative)|"
                                             r"downgrad.*credit rating"),
    (EventType.rating_change, None, "low", r"credit rating"),
    (EventType.merger_acquisition, None, "medium", r"acquisition|acquire|amalgamation|merger|scheme of arrangement|"
                                                   r"demerger|takeover|open offer|sale and transfer|divest|stake sale"),
    (EventType.capital_raise, "down", "low", r"qualified institution|\bqip\b|rights issue|preferential (?:issue|allotment|basis)"),
    (EventType.capital_raise, None, "low", r"fund ?raising|issue of (?:equity|shares|warrants)"),
    (EventType.other, None, "low", r"allotment of"),     # remaining allotments are routine (ESOPs etc.)
    (EventType.management_change, "down", "low", r"resignation of (?:the )?(?:statutory |secretarial )?auditor|"
                                                 r"resignation of (?:the )?(?:\w+ )?(?:ceo|md|managing director|chief executive|"
                                                 r"cfo|chief financial)"),
    (EventType.management_change, None, "low", r"resignation|appointment|change in (?:directors|management|kmp)|"
                                               r"cessation|key managerial|re-?appointment"),
    (EventType.guidance, None, "low", r"guidance|outlook"),
    (EventType.results, None, "medium", r"financial results?|outcome of board meeting|quarterly results|"
                                        r"audited results|unaudited results"),
    (EventType.other, None, "low", r"press release|release|launch|update"),
]
_COMPILED = [(e, d, s, re.compile(p, re.I)) for e, d, s, p in RULES]

_SUBJECT = re.compile(r"has informed the Exchange (?:about|regarding|that)?\s*(.*)", re.I | re.S)


@dataclass(frozen=True)
class RuleResult:
    event_type: EventType
    direction: str | None
    strength: str
    label: str                      # short derived label shown in the UI instead of NSE's text
    action_kind: str | None = None  # dividend | bonus | split | buyback | record date (equity only)
    action_date: str | None = None  # ISO date if the filing states one (a fact; the text isn't kept)


LABELS = {
    EventType.results: "Financial results", EventType.guidance: "Guidance/outlook",
    EventType.merger_acquisition: "Merger/acquisition filing", EventType.order_win: "Order/contract win",
    EventType.rating_change: "Credit rating update", EventType.regulatory_action: "Regulatory action/order",
    EventType.fraud_legal: "Fraud/default/legal filing", EventType.management_change: "Management change",
    EventType.capital_raise: "Capital raise/allotment", EventType.dividend_buyback: "Dividend/buyback",
    EventType.other: "Other filing",
}


_ACTIONS = [("buyback", r"buy-?back"), ("bonus", r"bonus (?:issue|shares?)|issue of bonus"),
            ("split", r"(?:stock )?split|sub-?division of (?:equity )?shares"),
            ("dividend", r"dividend(?! distribution policy)"), ("record date", r"record date|book closure")]
_ACTIONS_RE = [(k, re.compile(p, re.I)) for k, p in _ACTIONS]
_DEBT = re.compile(r"commercial paper|\bncds?\b|debenture|interest|principal|bond", re.I)
_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_DATE_PATTERNS = [
    re.compile(r"(\d{1,2})[-/ ]([A-Za-z]{3,9})[-/ ,]+(\d{4})"),          # 10-Oct-2026, 10 October 2026
    re.compile(r"([A-Za-z]{3,9}) (\d{1,2}),? (\d{4})"),                  # October 10, 2026
    re.compile(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})"),                 # 10/10/2026 (day first, Indian style)
]


def _find_date(text: str) -> str | None:
    """First plausible date after 'record date'/'ex-date'/'payment' in the text, as ISO."""
    from datetime import date
    m = re.search(r"(record date|ex-?date|book closure|payment date|date of payment)(.{0,80})", text, re.I | re.S)
    if not m:
        return None
    window = m.group(2)
    for i, pat in enumerate(_DATE_PATTERNS):
        d = pat.search(window)
        if not d:
            continue
        try:
            if i == 0:
                day, mon, year = int(d.group(1)), _MONTHS.get(d.group(2)[:3].lower()), int(d.group(3))
            elif i == 1:
                mon, day, year = _MONTHS.get(d.group(1)[:3].lower()), int(d.group(2)), int(d.group(3))
            else:
                day, mon, year = int(d.group(1)), int(d.group(2)), int(d.group(3))
            if mon:
                return date(year, mon, day).isoformat()
        except ValueError:
            continue
    return None


def corporate_action(subject: str, full: str) -> tuple[str | None, str | None]:
    """(kind, ISO date) for equity corporate actions; debt housekeeping is excluded."""
    if _DEBT.search(subject):
        return None, None
    for kind, pat in _ACTIONS_RE:
        if pat.search(subject):
            return kind, _find_date(full)
    return None, None


def classify_nse(description: str) -> RuleResult:
    """Map an NSE announcement description to an event type. The text is not retained;
    only the returned derived fields are."""
    m = _SUBJECT.search(description or "")
    subject = (m.group(1) if m else description or "").strip()
    kind, when = corporate_action(subject, description or "")
    for event, direction, strength, pat in _COMPILED:
        if pat.search(subject):
            label = LABELS[event]
            if event is EventType.rating_change and direction:
                label = f"Credit rating: {'upgrade/positive' if direction == 'up' else 'downgrade/negative'}"
            if event is EventType.dividend_buyback and kind:
                label = {"buyback": "Buyback", "bonus": "Bonus issue", "split": "Stock split",
                         "dividend": "Dividend", "record date": "Record date"}[kind]
            if event is EventType.management_change and direction == "down":
                label = "Key resignation (CEO/CFO/auditor)"
            if event is EventType.capital_raise and direction == "down":
                label = "Equity raise (QIP/preferential/rights)"
            return RuleResult(event, direction, strength, label, kind, when)
    return RuleResult(EventType.other, None, "low", LABELS[EventType.other], kind, when)
