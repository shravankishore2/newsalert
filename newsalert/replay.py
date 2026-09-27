"""Replay mode: run the alert engine over stored prices and measure it.

Metrics
- Latency: per alert, from the moment the price update is handed to the pipeline
  (received) to the moment the alert has been written to SQLite and passed to the
  sender (sent). In replay the sender is a stub, so this is in-process latency only;
  live Telegram latency is reported separately from live-mode alerts.
- False-alert rate: an alert is false if, within `false_alert_window_min` after it
  fires, the price gives back more than `reversal_frac` of the move (move measured
  from the reference price to the alert price). Alerts without a full forward window
  of same-session data are counted as not evaluable and excluded.
"""

from __future__ import annotations

import math
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .signals import Alert, Engine, Params
from .store import Store


@dataclass
class VariantResult:
    variant: str
    alerts: list[Alert]
    latencies_ms: list[float]
    verdicts: dict[tuple[str, int], bool | None]

    @property
    def evaluable(self) -> int:
        return sum(v is not None for v in self.verdicts.values())

    @property
    def false(self) -> int:
        return sum(v is True for v in self.verdicts.values())


def percentile(xs: list[float], q: float) -> float:
    s = sorted(xs)
    if not s:
        return math.nan
    k = (len(s) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (math.nan, math.nan)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    r = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((c - r) / d, (c + r) / d)


def classify(alert: Alert, forward: list[tuple[int, float]], window_s: int,
             frac: float, max_gap_s: int) -> bool | None:
    """True = false alert, False = held, None = not enough forward data to judge."""
    if not forward:
        return None
    prev = alert.ts
    for ts, _ in forward:
        if ts - prev > max_gap_s:
            return None  # session break or data hole inside the window
        prev = ts
    if alert.ts + window_s - forward[-1][0] > max_gap_s:
        return None  # window not covered
    give_back = frac * abs(alert.price - alert.ref_price)
    if alert.direction > 0:
        return min(p for _, p in forward) < alert.price - give_back
    return max(p for _, p in forward) > alert.price + give_back


class NullSender:
    def send(self, alert: Alert) -> None:
        pass


def run_variant(source: Store, table: str, params: Params, index_symbol: str,
                sink: Store, run_id: str, variant: str) -> tuple[list[Alert], list[float]]:
    engine = Engine(params, index_symbol)
    sender = NullSender()
    alerts, lat = [], []
    for symbol, ts, price in source.iter_bars(table, index_symbol):
        received = time.perf_counter_ns()
        a = engine.on_price(symbol, ts, price)
        if a is None:
            continue
        sink.insert_alert(a, mode="replay", run_id=run_id, variant=variant,
                          received_ns=received, sent_ns=None, delivered=False)
        sender.send(a)
        sent = time.perf_counter_ns()
        alerts.append(a)
        lat.append((sent - received) / 1e6)
    return alerts, lat


def replay(source: Store, table: str, alerts_cfg: dict, replay_cfg: dict, index_symbol: str,
           sink: Store) -> tuple[str, dict[str, VariantResult]]:
    run_id = "replay-" + uuid.uuid4().hex[:8]
    window_s = int(60 * replay_cfg["false_alert_window_min"])
    max_gap_s = int(60 * alerts_cfg["max_ref_staleness_min"])
    out = {}
    for variant, filters in (("unfiltered", False), ("filtered", True)):
        params = Params.from_config(alerts_cfg, filters=filters)
        alerts, lat = run_variant(source, table, params, index_symbol, sink, run_id, variant)
        verdicts = {}
        for a in alerts:
            fwd = source.prices_between(table, a.symbol, a.ts, a.ts + window_s)
            verdicts[(a.symbol, a.ts)] = classify(a, fwd, window_s, replay_cfg["reversal_frac"], max_gap_s)
        out[variant] = VariantResult(variant, alerts, lat, verdicts)
    return run_id, out


def _fmt_rate(k: int, n: int) -> str:
    if n == 0:
        return "n/a (no evaluable alerts)"
    lo, hi = wilson(k, n)
    return f"{k}/{n} = {k / n:.1%} (95% CI {lo:.1%}–{hi:.1%})"


def _fmt_ts(ts: int, tz) -> str:
    return datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d %H:%M %Z")


def render_results(*, title: str, source_desc: str, summary: tuple, trading_days: int, results: dict[str, VariantResult],
                   alerts_cfg: dict, replay_cfg: dict, live_latencies: list[float],
                   min_sample: int, generated: datetime, run_id: str, elapsed_s: float, tz=timezone.utc,
                   index_name: str = "the index", caveats: list[str] = ()) -> str:
    rows, n_symbols, t0, t1 = summary
    u, f = results["unfiltered"], results["filtered"]
    ma, cf = alerts_cfg["ma_filter"], alerts_cfg["corr_filter"]
    L = [
        f"## {title}",
        "",
        f"Generated {generated.strftime('%Y-%m-%d %H:%M UTC')} by `python -m newsalert replay` "
        f"(run `{run_id}`, {elapsed_s:.0f} s). All numbers below are measured, none estimated.",
        "",
        "### Data",
        "",
        f"- Source: {source_desc}",
        f"- Date range: {_fmt_ts(t0, tz)} to {_fmt_ts(t1, tz)}" if t0 else "- Date range: no data",
        f"- {trading_days} trading days; {rows:,} price points across {n_symbols} symbols (including the index)",
        "",
        "### Parameters (config.yaml, not tuned on this data)",
        "",
        f"- Candidate alert: |move| ≥ {alerts_cfg['move_threshold']:.1%} versus the price "
        f"{alerts_cfg['move_window_min']} min earlier; reference must be at most "
        f"{alerts_cfg['max_ref_staleness_min']} min older than that; "
        f"{alerts_cfg['cooldown_min']} min per-ticker cooldown; "
        f"{alerts_cfg['warmup_returns']}-sample warm-up. These apply to both variants.",
        f"- MA filter: {ma['fast_min']}-min SMA must be on the move's side of the {ma['slow_min']}-min SMA "
        f"and must itself have moved ≥ {ma['confirm_frac']}× threshold between the reference time and now.",
        f"- Correlation filter: over the last {cf['returns']} returns, if corr with {index_name} ≥ "
        f"{cf['min_corr']}, the move minus beta × index move must still clear the threshold.",
        "",
    ]
    L += ["### 1. Alert latency", "", "#### Replay (in-process)", "",
          "Measured from the moment each price update enters the pipeline to the moment the alert is "
          "committed to SQLite and handed to the sender. The sender in replay is a stub, so this "
          "**does not include Telegram network time**. Measured on the machine that ran the replay.", "",
          "| Variant | Alerts | p50 | p95 |", "|---|---:|---:|---:|"]
    for r in (u, f):
        if r.latencies_ms:
            L.append(f"| {r.variant} | {len(r.latencies_ms)} | {percentile(r.latencies_ms, .5):.3f} ms "
                     f"| {percentile(r.latencies_ms, .95):.3f} ms |")
        else:
            L.append(f"| {r.variant} | 0 | n/a | n/a |")
    L += ["", "#### Live (end to end, including Telegram)", ""]
    if live_latencies:
        L += [f"From {len(live_latencies)} delivered live alerts in `alerts.db`: time from the price "
              "response being parsed to Telegram `sendMessage` being acknowledged.", "",
              f"- p50: {percentile(live_latencies, .5):.1f} ms",
              f"- p95: {percentile(live_latencies, .95):.1f} ms"]
        if len(live_latencies) < min_sample:
            L.append(f"- **Sample-size warning:** fewer than {min_sample} live alerts.")
    else:
        L.append("**Not measured.** No delivered live alerts exist in `alerts.db` yet, so there is no "
                 "end-to-end number. Run live mode during market hours to collect one.")

    L += [
        "",
        "### 2. False-alert rate",
        "",
        f"**Definition.** An alert is *false* if, within {replay_cfg['false_alert_window_min']} minutes "
        f"after it fires, the price reverses by more than {replay_cfg['reversal_frac']:.0%} of the move "
        "(move = alert price − reference price). Prices are 1-minute bar closes. Alerts whose "
        f"{replay_cfg['false_alert_window_min']}-minute forward window is not fully covered by "
        "same-session data (e.g. near the close) are *not evaluable* and are left out of the rate.",
        "",
        "| Variant | Alerts | Evaluable | False-alert rate |",
        "|---|---:|---:|---|",
    ]
    for r, label in ((u, "Without filters"), (f, "With MA + correlation filters")):
        L.append(f"| {label} | {len(r.alerts)} | {r.evaluable} | {_fmt_rate(r.false, r.evaluable)} |")

    kept = set(f.verdicts)
    rej = [v for k, v in u.verdicts.items() if k not in kept and v is not None]
    kept_u = [v for k, v in u.verdicts.items() if k in kept and v is not None]
    new = [v for k, v in f.verdicts.items() if k not in u.verdicts and v is not None]
    L += [
        "",
        "**Where the difference comes from.** A filter rejection starts the per-ticker cooldown just "
        "like an alert does, so the filtered alerts are a subset of the unfiltered ones. Each "
        "unfiltered alert was either kept or rejected by the filters; these groups don't overlap:",
        "",
        f"- Kept by the filters: {_fmt_rate(sum(kept_u), len(kept_u))}",
        f"- Rejected by the filters: {_fmt_rate(sum(rej), len(rej))}",
    ]
    if new:
        L.append(f"- Filtered-run alerts with no unfiltered twin: {_fmt_rate(sum(new), len(new))}. These "
                 "come from candidates the filtered run couldn't evaluate (e.g. stale index data), which "
                 "don't start its cooldown.")
    L.append("")
    small = [lbl for r, lbl in ((u, "unfiltered"), (f, "filtered")) if r.evaluable < min_sample]
    if small:
        L += [f"**Sample-size warning:** fewer than {min_sample} evaluable alerts for: "
              f"{', '.join(small)}. Treat that rate as indicative only; the confidence interval shows how wide it is.", ""]

    L += ["### Caveats", ""] + [f"- {c}" for c in caveats] + [
        "- Reversals are judged on 1-minute closes, so a reversal that happens and recovers "
        "within one minute is not counted.", ""]
    return "\n".join(L)


RESULTS_HEADER = "# Results\n\nOne section per market. Each is regenerated by its own replay run; the others are left as they are.\n"


def _markers(key: str) -> tuple[str, str]:
    return f"<!-- results:{key}:start -->", f"<!-- results:{key}:end -->"


def write_results(path: str | Path, key: str, section: str) -> None:
    """Replace (or append) the `key` section of the results file, keeping other sections."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    text = p.read_text() if p.exists() else RESULTS_HEADER
    start, end = _markers(key)
    block = f"{start}\n{section.rstrip()}\n{end}"
    if start in text and end in text:
        i, j = text.index(start), text.index(end) + len(end)
        text = text[:i] + block + text[j:]
    else:
        # new sections go right after the header, above older ones
        first = text.find("<!-- results:")
        text = (text[:first] + block + "\n\n" + text[first:]) if first >= 0 else text.rstrip() + "\n\n" + block + "\n"
    p.write_text(text)
