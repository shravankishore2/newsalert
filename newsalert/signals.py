"""Alert logic, shared by live and replay mode.

For each price update the engine:
1. Finds the reference price `move_window_min` ago. If that reference is missing or
   stale (e.g. across an overnight gap or after failed fetches), no alert.
2. Candidate alert if |price/ref - 1| >= move_threshold.
3. MA trend filter: the fast SMA must sit on the move's side of the slow SMA, and the
   fast SMA must itself have moved >= confirm_frac * threshold between the reference
   time and now. A single-print spike (at either end of the window) moves the price
   but barely moves the fast SMA, so it is dropped. Limit: a one-bar spike larger than
   about fast_min_bars * confirm_frac * threshold still moves the SMA enough to pass.
4. Correlation filter: over the last N returns, compute corr and beta vs the index.
   If corr >= min_corr, subtract beta * index_move from the move; alert only if the
   residual still clears the threshold in the same direction. Market-wide moves drop.
   If the index data is stale the filter cannot be evaluated, so no alert this cycle.
5. Per-ticker cooldown. It starts on every *decided* candidate: one that alerts or one
   that a filter rejects. So a move the filters reject can't fire a few bars later
   instead, and the filtered alerts are a subset of the unfiltered ones. Candidates
   that can't be evaluated (stale index, too little history) don't start it.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Params:
    move_window_min: float = 15
    move_threshold: float = 0.015
    max_ref_staleness_min: float = 10
    cooldown_min: float = 30
    warmup_returns: int = 30
    ma_enabled: bool = True
    ma_fast_min: float = 5
    ma_slow_min: float = 60
    ma_confirm_frac: float = 0.5
    corr_enabled: bool = True
    corr_returns: int = 30
    corr_min: float = 0.6
    index_max_staleness_min: float = 3

    @classmethod
    def from_config(cls, a: dict[str, Any], *, filters: bool = True) -> "Params":
        ma, cf = a["ma_filter"], a["corr_filter"]
        return cls(
            move_window_min=a["move_window_min"], move_threshold=a["move_threshold"],
            max_ref_staleness_min=a["max_ref_staleness_min"], cooldown_min=a["cooldown_min"],
            warmup_returns=a["warmup_returns"],
            ma_enabled=filters and ma["enabled"], ma_fast_min=ma["fast_min"],
            ma_slow_min=ma["slow_min"], ma_confirm_frac=ma["confirm_frac"],
            corr_enabled=filters and cf["enabled"], corr_returns=cf["returns"],
            corr_min=cf["min_corr"], index_max_staleness_min=cf["index_max_staleness_min"],
        )


@dataclass(frozen=True)
class Alert:
    symbol: str
    ts: int
    direction: int  # +1 up, -1 down
    move: float
    ref_ts: int
    ref_price: float
    price: float
    index_move: float | None = None
    corr: float | None = None
    beta: float | None = None
    # Why the alert passed (None when that filter was disabled):
    fast_sma: float | None = None       # MA filter: fast SMA now
    slow_sma: float | None = None       # MA filter: slow SMA now
    fast_move: float | None = None      # MA filter: fast SMA now vs at the reference time
    residual: float | None = None       # corr filter: move - beta * index move (if corr >= min_corr)


class Series:
    """Time-ordered prices for one symbol, pruned to a bounded history."""

    def __init__(self, keep_seconds: float, keep_min_points: int):
        self.ts: list[int] = []
        self.px: list[float] = []
        self._keep_s, self._keep_n = keep_seconds, keep_min_points

    def add(self, ts: int, price: float) -> bool:
        if self.ts and ts <= self.ts[-1]:
            return False  # not newer: stale or duplicate update
        self.ts.append(ts)
        self.px.append(price)
        if len(self.ts) > 4 * self._keep_n + 1000:
            self._prune()
        return True

    def _prune(self) -> None:
        cut = bisect_right(self.ts, self.ts[-1] - self._keep_s)
        cut = min(cut, len(self.ts) - self._keep_n - 1)
        if cut > 0:
            del self.ts[:cut], self.px[:cut]

    def at_or_before(self, ts: float) -> int:
        """Index of the last point with timestamp <= ts, or -1."""
        return bisect_right(self.ts, ts) - 1

    def mean_between(self, start_ts: float, end_ts: float) -> float:
        """Mean of points with start_ts < ts <= end_ts (falls back to the last point at/before end_ts)."""
        i, j = bisect_right(self.ts, start_ts), bisect_right(self.ts, end_ts)
        pts = self.px[i:j] or self.px[max(j - 1, 0):j]
        return sum(pts) / len(pts)


def _corr_beta(xs: list[float], ys: list[float]) -> tuple[float, float] | None:
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    if sxx <= 0 or syy <= 0:
        return None
    return sxy / math.sqrt(sxx * syy), sxy / syy  # corr, beta of x on y


class Engine:
    def __init__(self, params: Params, index_symbol: str):
        self.p = params
        self.index_symbol = index_symbol
        # Correlation looks back a fixed number of *ticker* returns, which at a coarse
        # cadence or across an overnight/weekend gap can span days. Every series (the
        # index in particular, which updates most often) must keep at least that much,
        # or early-session candidates can't be evaluated. 4 days covers a weekend.
        keep_s = 60 * max(params.move_window_min + params.max_ref_staleness_min, params.ma_slow_min) + 4 * 86400
        self._keep = (keep_s, 2 * max(params.corr_returns, params.warmup_returns) + 2)
        self.series: dict[str, Series] = {}
        self.last_alert: dict[str, int] = {}  # last decided candidate per symbol (cooldown)
        self.rejected = 0

    def _series(self, symbol: str) -> Series:
        s = self.series.get(symbol)
        if s is None:
            s = self.series[symbol] = Series(*self._keep)
        return s

    def _reject(self, symbol: str, ts: int) -> None:
        self.last_alert[symbol] = ts
        self.rejected += 1
        return None

    def on_price(self, symbol: str, ts: int, price: float) -> Alert | None:
        s = self._series(symbol)
        if not s.add(ts, price) or symbol == self.index_symbol:
            return None
        return self._evaluate(symbol, s, ts, price)

    def _returns(self, s: Series, n: int) -> tuple[list[float], list[float]] | None:
        """Last n (ticker, index) return pairs over the ticker's own sample intervals.
        Intervals longer than the staleness limit (overnight gaps, fetch gaps) are skipped."""
        idx = self.series.get(self.index_symbol)
        if idx is None:
            return None
        max_gap = 60 * self.p.max_ref_staleness_min
        xs, ys = [], []
        j = len(s.ts) - 1
        while j > 0 and len(xs) < n:
            t0, t1 = s.ts[j - 1], s.ts[j]
            if t1 - t0 <= max_gap:
                i0, i1 = idx.at_or_before(t0), idx.at_or_before(t1)
                if i0 >= 0 and i1 >= 0 and t0 - idx.ts[i0] <= max_gap and t1 - idx.ts[i1] <= max_gap:
                    xs.append(s.px[j] / s.px[j - 1] - 1)
                    ys.append(idx.px[i1] / idx.px[i0] - 1)
            j -= 1
        return (xs, ys) if len(xs) >= n else None

    def _evaluate(self, symbol: str, s: Series, ts: int, price: float) -> Alert | None:
        p = self.p
        if len(s.ts) <= p.warmup_returns:
            return None
        window = 60 * p.move_window_min
        r = s.at_or_before(ts - window)
        if r < 0 or ts - s.ts[r] > window + 60 * p.max_ref_staleness_min:
            return None  # no usable reference price
        ref_ts, ref = s.ts[r], s.px[r]
        move = price / ref - 1
        if abs(move) < p.move_threshold:
            return None
        direction = 1 if move > 0 else -1
        last = self.last_alert.get(symbol)
        if last is not None and ts - last < 60 * p.cooldown_min:
            return None

        fast = slow = fast_move = residual = None
        if p.ma_enabled:
            fast_s, slow_s = 60 * p.ma_fast_min, 60 * p.ma_slow_min
            fast = s.mean_between(ts - fast_s, ts)
            slow = s.mean_between(ts - slow_s, ts)
            fast_at_ref = s.mean_between(ref_ts - fast_s, ref_ts)
            if direction * (fast - slow) <= 0:
                return self._reject(symbol, ts)
            # The fast SMA itself must have moved, measured SMA-to-SMA so a single bad
            # print at either end (current price or reference price) can't pass.
            fast_move = fast / fast_at_ref - 1
            if direction * fast_move < p.ma_confirm_frac * p.move_threshold:
                return self._reject(symbol, ts)

        index_move = corr = beta = None
        if p.corr_enabled:
            idx = self.series.get(self.index_symbol)
            if idx is None or not idx.ts or ts - idx.ts[-1] > 60 * p.index_max_staleness_min:
                return None  # stale index: skip rather than judge on stale data
            i0 = idx.at_or_before(ref_ts)
            if i0 < 0 or ref_ts - idx.ts[i0] > 60 * p.max_ref_staleness_min:
                return None
            index_move = idx.px[-1] / idx.px[i0] - 1
            rets = self._returns(s, p.corr_returns)
            if rets is None:
                return None
            cb = _corr_beta(*rets)
            if cb is not None:
                corr, beta = cb
                if corr >= p.corr_min:
                    residual = move - beta * index_move
                    if direction * residual < p.move_threshold:
                        return self._reject(symbol, ts)

        self.last_alert[symbol] = ts
        return Alert(symbol, ts, direction, move, ref_ts, ref, price, index_move, corr, beta,
                     fast, slow, fast_move, residual)
