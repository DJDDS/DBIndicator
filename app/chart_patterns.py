"""
Chart Pattern Scanner - timeframe-matched classical pattern detection.

Principle (see the Timeframe Guide on the /patterns page): a pattern's
reliability comes from how long it took to form, so each pattern is only
searched on the timeframes where it is dependable:

    Reversal patterns (H&S, cup & handle, triple)  -> Weekly, Daily
    Double top / bottom                            -> Daily, 4H
    Triangles                                      -> Daily, 4H, 1H
    Wedges                                         -> Daily, 4H
    Rectangles, flags & pennants                   -> Daily, 1H

Every detector is written once, in its BULLISH orientation (inverse H&S,
double bottom, cup & handle, ascending triangle, falling wedge, bull flag,
upside rectangle/symmetrical break). Bearish twins are found by running the
same detector on a price-mirrored frame (high -> -low, low -> -high), which
guarantees the bull and bear rules are exactly symmetric.

Each hit is scored 0-100 from: timeframe reliability, geometric fit, volume
confirmation, higher-timeframe trend alignment, reward:risk and pattern
length. Status is one of:
    FORMING   - structure complete, price within 1 ATR of the trigger
    BREAKOUT  - closed through the trigger within the last 3 bars
    EXTENDED  - fresh breakout but already > 1.5 ATR past trigger (chase guard)

This module only spots and ranks patterns. It never places orders.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import math
import os
import threading
import time
from dataclasses import dataclass

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
TIMEFRAMES = ["week", "day", "4hour", "60minute"]
TF_LABEL = {"week": "Weekly", "day": "Daily", "4hour": "4H", "60minute": "1H"}
TF_WEIGHT = {"week": 30, "day": 26, "4hour": 20, "60minute": 16}   # reliability pts /30
PIVOT_ORDER = {"week": 3, "day": 5, "4hour": 4, "60minute": 5}
MIN_BARS = {"week": 10, "day": 15, "4hour": 15, "60minute": 15}
LOOKBACK_BARS = {"week": 150, "day": 260, "4hour": 200, "60minute": 220}

FAMILIES = {
    "head_shoulders": {"label": "Head & Shoulders", "kind": "reversal"},
    "double": {"label": "Double Top / Bottom", "kind": "reversal"},
    "triple": {"label": "Triple Top / Bottom", "kind": "reversal"},
    "cup_handle": {"label": "Cup & Handle", "kind": "continuation"},
    "triangle": {"label": "Triangles", "kind": "continuation"},
    "wedge": {"label": "Wedges", "kind": "reversal"},
    "rectangle": {"label": "Rectangle / Range", "kind": "continuation"},
    "flag": {"label": "Flag / Pennant", "kind": "continuation"},
}

# Which pattern family is searched on which timeframe.
# "primary" = most effective, "ok" = valid but weaker.
PATTERN_MATRIX = {
    "head_shoulders": {"week": "primary", "day": "primary"},
    "cup_handle": {"week": "primary", "day": "primary"},
    "triple": {"week": "ok", "day": "primary"},
    "double": {"day": "primary", "4hour": "ok"},
    "triangle": {"day": "primary", "4hour": "primary", "60minute": "ok"},
    "wedge": {"day": "primary", "4hour": "ok"},
    "rectangle": {"day": "primary", "60minute": "ok"},
    "flag": {"day": "primary", "60minute": "primary"},
}

MATRIX_NOTES = {
    "head_shoulders": "Major reversal - needs weeks of distribution/accumulation. Intraday versions fail often.",
    "cup_handle": "Cup 7-65 weeks, handle 1-4 weeks. Intraday 'cups' are mostly noise.",
    "triple": "Three tests of one level; weekly triples mark multi-month floors/ceilings.",
    "double": "Peaks/troughs should sit 2+ weeks apart; 4H works for swing timing.",
    "triangle": "Continuation pause - works across timeframes; daily/4H most dependable.",
    "wedge": "Converging trend exhaustion; needs 3-6 weeks on daily.",
    "rectangle": "Range breakout - valid on any timeframe with volume.",
    "flag": "Short pause after a sharp pole - the best pattern for lower timeframes.",
}

FRESH_BARS = 3
CHASE_ATR = 1.5
FORMING_ATR = 1.0

INDEX_SYMBOLS = ["NIFTY 50", "NIFTY BANK", "NIFTY FIN SERVICE"]


def _default_results_file():
    explicit = os.getenv("PATTERN_RESULTS_FILE")
    if explicit:
        return explicit
    base = os.path.dirname(os.getenv("SCAN_RESULTS_FILE", "") or "")
    return os.path.join(base or ".", "pattern_scan_results.json")


RESULTS_FILE = _default_results_file()


# ---------------------------------------------------------------------------
# Frame helpers
# ---------------------------------------------------------------------------
@dataclass
class Frame:
    t: np.ndarray       # epoch seconds
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    v: np.ndarray
    atr: np.ndarray
    sign: int = 1       # +1 normal, -1 mirrored (bearish search)

    @property
    def n(self):
        return len(self.c)


def _atr(h, l, c, length=14):
    prev = np.concatenate([[c[0]], c[:-1]])
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev), np.abs(l - prev)))
    return pd.Series(tr).ewm(alpha=1.0 / length, adjust=False).mean().to_numpy()


def make_frame(df: pd.DataFrame) -> Frame | None:
    if df is None or df.empty or len(df) < 30:
        return None
    df = df.dropna(subset=["open", "high", "low", "close"])
    idx = df.index
    t = np.array([int(pd.Timestamp(x).timestamp()) for x in idx], dtype=np.int64)
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)
    o = df["open"].to_numpy(float)
    v = df["volume"].to_numpy(float) if "volume" in df else np.zeros(len(df))
    return Frame(t, o, h, l, c, np.nan_to_num(v), _atr(h, l, c))


def mirror(fr: Frame) -> Frame:
    return Frame(fr.t, -fr.o, -fr.l, -fr.h, -fr.c, fr.v, fr.atr, -fr.sign)


def find_pivots(h, l, k):
    """Alternating swing highs/lows. Each pivot is (index, price, 'H'|'L')."""
    n = len(h)
    raw = []
    for i in range(k, n - k):
        if h[i] >= h[i - k:i + k + 1].max():
            raw.append((i, float(h[i]), "H"))
        if l[i] <= l[i - k:i + k + 1].min():
            raw.append((i, float(l[i]), "L"))
    out = []
    for p in raw:
        if out and out[-1][2] == p[2]:
            better = p[1] >= out[-1][1] if p[2] == "H" else p[1] <= out[-1][1]
            if better:
                out[-1] = p
        else:
            out.append(p)
    return out


class Line:
    def __init__(self, i0, p0, i1, p1):
        self.slope = (p1 - p0) / (i1 - i0) if i1 != i0 else 0.0
        self.i0, self.p0 = i0, p0

    def __call__(self, i):
        return self.p0 + self.slope * (i - self.i0)


class FitLine(Line):
    def __init__(self, xs, ys):
        xs = np.asarray(xs, float)
        ys = np.asarray(ys, float)
        if len(xs) >= 3:
            slope, icpt = np.polyfit(xs, ys, 1)
        else:
            slope = (ys[1] - ys[0]) / (xs[1] - xs[0])
            icpt = ys[0] - slope * xs[0]
        self.slope, self.i0, self.p0 = float(slope), 0, float(icpt)
        self.resid = float(np.max(np.abs(ys - (icpt + slope * xs))))


def _mean(a):
    a = np.asarray(a, float)
    return float(a.mean()) if a.size else 0.0


# ---------------------------------------------------------------------------
# Breakout / status evaluation (bullish orientation: break = close ABOVE line)
# ---------------------------------------------------------------------------
def _status(fr: Frame, line, from_i, stop):
    c, a = fr.c, float(fr.atr[-1])
    last = fr.n - 1
    if a <= 0 or from_i > last:
        return None
    j = None
    for i in range(max(from_i, 1), last + 1):
        if c[i] > line(i) + 0.02 * a:
            j = i
            break
    if j is None:
        if c[last] <= stop:
            return None
        dist = (line(last) - c[last]) / a
        if dist <= FORMING_ATR:
            return {"status": "FORMING", "dist_atr": round(dist, 2), "break_i": None}
        return None
    if last - j >= FRESH_BARS or c[last] <= line(last):
        return None   # stale, or failed back inside the pattern
    ext = (c[last] - line(last)) / a
    return {"status": "EXTENDED" if ext > CHASE_ATR else "BREAKOUT",
            "dist_atr": round(-ext, 2), "break_i": j}


# ---------------------------------------------------------------------------
# Detectors - all in bullish orientation. Each returns a dict or None.
# ---------------------------------------------------------------------------
def detect_inverse_hs(fr: Frame, piv, tf):
    a = float(fr.atr[-1])
    cands = [p for p in piv if p[0] >= fr.n - LOOKBACK_BARS[tf]]
    for e in range(len(cands) - 1, 3, -1):
        seq = cands[e - 4:e + 1]
        if [p[2] for p in seq] != ["L", "H", "L", "H", "L"]:
            continue
        ls, n1, hd, n2, rs = seq
        neck = Line(n1[0], n1[1], n2[0], n2[1])
        height = neck(hd[0]) - hd[1]
        if height < 2 * a:
            continue
        if not (hd[1] < ls[1] - 0.5 * a and hd[1] < rs[1] - 0.5 * a):
            continue
        if abs(ls[1] - rs[1]) > 0.5 * height or abs(n2[1] - n1[1]) > 0.6 * height:
            continue
        span_l, span_r = hd[0] - ls[0], rs[0] - hd[0]
        if span_l <= 0 or span_r <= 0 or not 0.4 <= span_r / span_l <= 2.5:
            continue
        if rs[0] - ls[0] < MIN_BARS[tf]:
            continue
        pre = fr.h[max(0, ls[0] - span_l):ls[0] + 1]
        if pre.size == 0 or pre.max() < neck(ls[0]):   # must come from above the neckline
            continue
        if fr.l[rs[0]:].min() < hd[1]:
            continue
        stop = rs[1] - 0.25 * a
        st = _status(fr, neck, rs[0] + 1, stop)
        if not st:
            continue
        sym = 1 - abs(ls[1] - rs[1]) / (0.5 * height)
        tsym = 1 - abs(math.log(span_r / span_l)) / math.log(2.5)
        vol_l = _mean(fr.v[ls[0]:hd[0]])
        vol_r = _mean(fr.v[hd[0]:rs[0] + 1])
        vol_ok = vol_l > 0 and vol_r < vol_l
        fit = 0.5 * sym + 0.35 * tsym + (0.15 if vol_ok else 0)
        return {
            "key": "head_shoulders", "bull": "Inverse Head & Shoulders", "bear": "Head & Shoulders",
            "line": neck, "stop": stop, "height": height, "start_i": ls[0], "fit": fit, **st,
            "points": [(ls[0], ls[1], "LS"), (hd[0], hd[1], "H"), (rs[0], rs[1], "RS")],
            "segments": [[(ls[0], ls[1]), (n1[0], n1[1]), (hd[0], hd[1]), (n2[0], n2[1]), (rs[0], rs[1])]],
            "trigger_line": neck, "trigger_from": n1[0],
        }
    return None


def detect_double_triple(fr: Frame, piv, tf, want):
    a = float(fr.atr[-1])
    cands = [p for p in piv if p[0] >= fr.n - LOOKBACK_BARS[tf]]
    need = 5 if want == "triple" else 3
    pattern = ["L", "H", "L", "H", "L"][:need]
    for e in range(len(cands) - 1, need - 2, -1):
        seq = cands[e - need + 1:e + 1]
        if [p[2] for p in seq] != pattern:
            continue
        lows = [p for p in seq if p[2] == "L"]
        highs = [p for p in seq if p[2] == "H"]
        base = min(p[1] for p in lows)
        tol = max(0.6 * a, 0.01 * abs(base))
        if max(p[1] for p in lows) - base > tol:
            continue
        if any(lows[k + 1][0] - lows[k][0] < 8 for k in range(len(lows) - 1)):
            continue
        if lows[-1][0] - lows[0][0] < MIN_BARS[tf]:
            continue
        neck_p = max(p[1] for p in highs)
        depth = neck_p - base
        if depth < 2 * a:
            continue
        if want == "triple" and min(p[1] for p in highs) < base + 0.5 * depth:
            continue
        spacing = lows[1][0] - lows[0][0]
        pre = fr.h[max(0, lows[0][0] - spacing):lows[0][0] + 1]
        if pre.size == 0 or pre.max() < neck_p:
            continue
        if fr.l[lows[-1][0]:].min() < base - tol:
            continue
        neck = Line(0, neck_p, 1, neck_p)
        stop = base - 0.25 * a
        st = _status(fr, neck, lows[-1][0] + 1, stop)
        if not st:
            continue
        eq = 1 - (max(p[1] for p in lows) - base) / tol
        vol_ok = _mean(fr.v[lows[-1][0] - 2:lows[-1][0] + 3]) < _mean(fr.v[lows[0][0] - 2:lows[0][0] + 3])
        fit = 0.55 * eq + 0.3 * min(1.0, depth / (4 * a)) + (0.15 if vol_ok else 0)
        pts = [(p[0], p[1], "B" if p[2] == "L" else "N") for p in seq]
        return {
            "key": want,
            "bull": "Triple Bottom" if want == "triple" else "Double Bottom",
            "bear": "Triple Top" if want == "triple" else "Double Top",
            "line": neck, "stop": stop, "height": depth, "start_i": lows[0][0], "fit": fit, **st,
            "points": pts, "segments": [[(p[0], p[1]) for p in seq]],
            "trigger_line": neck, "trigger_from": seq[0][0],
        }
    return None


def detect_cup_handle(fr: Frame, piv, tf):
    a = float(fr.atr[-1])
    lo_len, hi_len = {"week": (7, 65), "day": (30, 250)}.get(tf, (20, 150))
    d_lo, d_hi = (0.08, 0.50) if tf in ("week", "day") else (0.03, 0.35)
    highs = [p for p in piv if p[2] == "H" and p[0] >= fr.n - LOOKBACK_BARS[tf] - hi_len]
    for ci in range(len(highs) - 1, 0, -1):
        C = highs[ci]
        if fr.n - 1 - C[0] > max(25, hi_len // 3):
            continue
        for ai in range(ci - 1, -1, -1):
            A = highs[ai]
            L = C[0] - A[0]
            if L < lo_len:
                continue
            if L > hi_len:
                break
            seg_h = fr.h[A[0]:C[0] + 1]
            if seg_h.max() > max(A[1], C[1]) + 0.1 * a:
                continue
            seg_l = fr.l[A[0]:C[0] + 1]
            b = int(np.argmin(seg_l))
            B = seg_l[b]
            depth = A[1] - B
            if depth <= 0 or not d_lo <= depth / abs(A[1]) <= d_hi:
                continue
            if not (A[1] - 0.12 * depth <= C[1] <= A[1] + 0.15 * depth):
                continue
            pos = b / L
            if not 0.2 <= pos <= 0.8:
                continue
            mid = A[1] - depth / 2
            round_frac = float(np.mean(fr.c[A[0]:C[0] + 1] < mid))
            if round_frac < 0.25:
                continue
            pivot = C[1]
            trig = Line(0, pivot, 1, pivot)
            j = None
            for i in range(C[0] + 1, fr.n):
                if fr.c[i] > pivot + 0.02 * a:
                    j = i
                    break
            h_end = (j - 1) if j is not None else fr.n - 1
            hlen = h_end - C[0]
            max_h = max(3, L // 3) if tf != "week" else max(4, L // 3)
            if hlen < 2 or hlen > max_h:
                continue
            hl = float(fr.l[C[0] + 1:h_end + 1].min())
            if C[1] - hl > 0.5 * depth or hl < mid:
                continue
            stop = hl - 0.25 * a
            st = _status(fr, trig, C[0] + 1, stop)
            if not st:
                continue
            vol_ok = _mean(fr.v[C[0] + 1:h_end + 1]) < _mean(fr.v[A[0]:C[0] + 1])
            rim = 1 - abs(C[1] - A[1]) / (0.15 * depth)
            shallow = 1 - (C[1] - hl) / (0.5 * depth)
            fit = 0.3 * min(1.0, round_frac / 0.45) + 0.25 * max(0.0, rim) + 0.3 * shallow + (0.15 if vol_ok else 0)
            bi = A[0] + b
            return {
                "key": "cup_handle", "bull": "Cup & Handle", "bear": "Inverted Cup & Handle",
                "line": trig, "stop": stop, "height": depth, "start_i": A[0], "fit": fit, **st,
                "points": [(A[0], A[1], "Rim"), (bi, B, "Cup"), (C[0], C[1], "Rim"),
                           (C[0] + 1 + int(np.argmin(fr.l[C[0] + 1:h_end + 1])), hl, "Handle")],
                "segments": [[(A[0], A[1]), (bi, B), (C[0], C[1])]],
                "trigger_line": trig, "trigger_from": A[0],
            }
    return None


def detect_line_patterns(fr: Frame, piv, tf):
    """Ascending / symmetrical triangle, falling wedge, rectangle (bull side)."""
    a = float(fr.atr[-1])
    win = [p for p in piv if p[0] >= fr.n - LOOKBACK_BARS[tf] // 2]
    best = None
    for take in (4, 3, 2):
        hs = [p for p in win if p[2] == "H"][-take:]
        ls = [p for p in win if p[2] == "L"][-take:]
        if len(hs) < 2 or len(ls) < 2 or len(hs) + len(ls) < 4:
            continue
        start = min(hs[0][0], ls[0][0])
        end = max(hs[-1][0], ls[-1][0])
        span = end - start
        if span < MIN_BARS[tf] or fr.n - 1 - end > max(10, span // 2):
            continue
        up = FitLine([p[0] for p in hs], [p[1] for p in hs])
        lo = FitLine([p[0] for p in ls], [p[1] for p in ls])
        if up.resid > 0.6 * a or lo.resid > 0.6 * a:
            continue
        du, dl = up.slope * span / a, lo.slope * span / a
        w0, w1 = up(start) - lo(start), up(end) - lo(end)
        if w0 <= 0 or w1 <= 0 or up(fr.n - 1) <= lo(fr.n - 1):
            continue
        conv = w1 / w0
        flat_u, flat_l = abs(du) <= 1.0, abs(dl) <= 1.0
        if flat_u and flat_l and w0 >= 2 * a and 0.65 <= conv <= 1.35:
            key, bull, bear, kind_tgt = "rectangle", "Rectangle Breakout", "Rectangle Breakdown", "height"
        elif flat_u and dl > 1.0 and conv < 0.75:
            key, bull, bear, kind_tgt = "triangle", "Ascending Triangle", "Descending Triangle", "height"
        elif du < -1.0 and dl > 1.0 and conv < 0.75:
            key, bull, bear, kind_tgt = "triangle", "Symmetrical Triangle (up-break)", "Symmetrical Triangle (down-break)", "height"
        elif du < -1.0 and dl < -1.0 and up.slope < lo.slope and conv < 0.8:
            key, bull, bear, kind_tgt = "wedge", "Falling Wedge", "Rising Wedge", "origin"
        else:
            continue
        # price must have respected the structure between start and end
        inside = fr.c[start:end + 1]
        xs = np.arange(start, end + 1)
        outside = np.sum((inside > up(xs) + 0.3 * a) | (inside < lo(xs) - 0.3 * a))
        if outside > 2:
            continue
        stop = min(lo(fr.n - 1), ls[-1][1]) - 0.25 * a
        st = _status(fr, up, end + 1, stop)
        if not st:
            continue
        touches = len(hs) + len(ls)
        fit = 0.55 * (1 - (up.resid + lo.resid) / (1.2 * a)) + 0.3 * min(1.0, (touches - 3) / 3) + 0.15 * (1 - min(conv, 1.0))
        height = w0 if kind_tgt == "height" else (up(start) - up(fr.n - 1))
        res = {
            "key": key, "bull": bull, "bear": bear, "line": up, "stop": stop,
            "height": max(height, w0 * 0.5), "start_i": start, "fit": fit, **st,
            "points": [(p[0], p[1], "") for p in hs + ls],
            "segments": [[(start, up(start)), (fr.n - 1, up(fr.n - 1))],
                         [(start, lo(start)), (fr.n - 1, lo(fr.n - 1))]],
            "trigger_line": up, "trigger_from": start,
        }
        if best is None or res["fit"] > best["fit"]:
            best = res
    return best


def detect_flag(fr: Frame, piv, tf):
    a = float(fr.atr[-1])
    max_pole = {"week": 8, "day": 15, "4hour": 15, "60minute": 20}[tf]
    max_cons = {"week": 6, "day": 20, "4hour": 20, "60minute": 30}[tf]
    n = fr.n
    h, l, c, v = fr.h, fr.l, fr.c, fr.v
    for p in range(n - 3, max(max_pole, n - 2 - max_cons - FRESH_BARS), -1):
        s = p - max_pole + int(np.argmin(l[p - max_pole:p + 1]))
        pole = h[p] - l[s]
        bars = p - s
        if bars < 2 or pole < 3 * a or pole / bars < 0.5 * a:
            continue
        if h[s:p + 1].max() > h[p]:
            continue
        # walk consolidation bars, building the upper envelope from the pole top
        slope, j = -1e18, None
        for i in range(p + 1, n):
            if i - p - 1 >= 3:
                line_i = h[p] + slope * (i - p)
                if c[i] > line_i + 0.02 * a:
                    j = i
                    break
            slope = max(slope, (h[i] - h[p]) / (i - p))
        end = (j - 1) if j is not None else n - 1
        clen = end - p
        if clen < 3 or clen > max_cons:
            continue
        if slope * clen > 0.3 * a:
            continue   # consolidation must not rise above the pole top
        cl = l[p + 1:end + 1]
        low = float(cl.min())
        if h[p] - low > 0.5 * pole:
            continue
        upper = Line(p, h[p], p + 1, h[p] + slope)
        stop = low - 0.2 * a
        st = _status(fr, upper, p + 1, stop)
        if not st:
            continue
        lslope = np.polyfit(np.arange(len(cl)), cl, 1)[0] if len(cl) >= 3 else 0.0
        pennant = slope < 0 and lslope > 0
        vol_ok = _mean(v[p + 1:end + 1]) < _mean(v[s:p + 1])
        retr = (h[p] - low) / pole
        fit = 0.45 * (1 - retr / 0.5) + 0.25 * min(1.0, pole / (6 * a)) + (0.3 if vol_ok else 0.1)
        return {
            "key": "flag",
            "bull": "Bull Pennant" if pennant else "Bull Flag",
            "bear": "Bear Pennant" if pennant else "Bear Flag",
            "line": upper, "stop": stop, "height": pole, "start_i": s, "fit": fit, **st,
            "points": [(s, l[s], "Pole"), (p, h[p], "")],
            "segments": [[(s, l[s]), (p, h[p])],
                         [(p, h[p]), (n - 1, upper(n - 1))],
                         [(p + 1, low), (n - 1, low)]],
            "trigger_line": upper, "trigger_from": p,
        }
    return None


DETECTORS = {
    "head_shoulders": detect_inverse_hs,
    "double": lambda fr, pv, tf: detect_double_triple(fr, pv, tf, "double"),
    "triple": lambda fr, pv, tf: detect_double_triple(fr, pv, tf, "triple"),
    "cup_handle": detect_cup_handle,
    "flag": detect_flag,
}
LINE_FAMILIES = {"triangle", "wedge", "rectangle"}


# ---------------------------------------------------------------------------
# Scoring and packaging
# ---------------------------------------------------------------------------
def trend_of(fr: Frame | None, ema_len=20):
    if fr is None or fr.n < ema_len + 6:
        return "NEUTRAL"
    ema = pd.Series(fr.c).ewm(span=ema_len, adjust=False).mean().to_numpy()
    rising = ema[-1] > ema[-6]
    if fr.c[-1] > ema[-1] and rising:
        return "UP"
    if fr.c[-1] < ema[-1] and not rising:
        return "DOWN"
    return "NEUTRAL"


def _grade(score):
    return "A" if score >= 75 else "B" if score >= 60 else "C"


def _package(symbol, tf, fr: Frame, raw, htf_trend, matrix_level):
    s = fr.sign
    bull = s == 1
    a = float(fr.atr[-1])
    last = fr.n - 1
    line = raw["line"]
    bi = raw.get("break_i")
    trig_level = line(bi) if bi is not None else line(last)
    entry = fr.c[last] if bi is not None else trig_level
    target = trig_level + raw["height"]
    stop = raw["stop"]
    risk = entry - stop
    rr = (target - entry) / risk if risk > 0 else 0.0
    if rr <= 0:
        return None

    # volume confirmation
    vol_ratio = None
    if fr.v[-30:].sum() > 0:
        if bi is not None:
            base = _mean(fr.v[max(0, bi - 20):bi])
            vol_ratio = fr.v[bi] / base if base > 0 else None
        else:
            base = _mean(fr.v[-25:-5])
            vol_ratio = _mean(fr.v[-5:]) / base if base > 0 else None

    fam = raw["key"]
    kind = FAMILIES[fam]["kind"]
    direction = "BULL" if bull else "BEAR"
    aligned = (htf_trend == "UP" and bull) or (htf_trend == "DOWN" and not bull)
    opposed = (htf_trend == "UP" and not bull) or (htf_trend == "DOWN" and bull)

    tf_pts = TF_WEIGHT[tf] * (1.0 if matrix_level == "primary" else 0.85)
    fit_pts = 25 * max(0.0, min(1.0, raw["fit"]))
    if vol_ratio is None:
        vol_pts, vol_note = 7, "n/a"
    elif bi is not None:
        vol_pts = 15 if vol_ratio >= 1.5 else 9 if vol_ratio >= 1.2 else 3
        vol_note = "confirmed" if vol_ratio >= 1.5 else "weak" if vol_ratio >= 1.2 else "unconfirmed"
    else:
        vol_pts = 10 if vol_ratio <= 0.8 else 6 if vol_ratio <= 1.0 else 3
        vol_note = "drying up" if vol_ratio <= 0.8 else "normal" if vol_ratio <= 1.0 else "elevated"
    if kind == "continuation":
        trend_pts = 15 if aligned else 0 if opposed else 8
    else:
        trend_pts = 15 if aligned else 5 if opposed else 10
    rr_pts = 10 * min(rr / 3.0, 1.0)
    bars = last - raw["start_i"]
    len_pts = 5 if bars >= MIN_BARS[tf] * 1.5 else 3
    score = round(tf_pts + fit_pts + vol_pts + trend_pts + rr_pts + len_pts)
    if raw["status"] == "EXTENDED":
        score -= 5

    def P(x):
        return round(float(x) * s, 2)

    start_bar = max(0, raw["start_i"] - 15)
    candles = [
        {"time": int(fr.t[i]), "open": P(fr.o[i]),
         "high": P(fr.h[i] if bull else fr.l[i]), "low": P(fr.l[i] if bull else fr.h[i]),
         "close": P(fr.c[i])}
        for i in range(start_bar, fr.n)
    ][-320:]
    segments = [[{"time": int(fr.t[i]), "value": P(p)} for i, p in seg] for seg in raw["segments"]]
    points = [{"time": int(fr.t[i]), "price": P(p), "label": lab} for i, p, lab in raw["points"]]

    name = raw["bull"] if bull else raw["bear"]
    return {
        "id": f"{symbol}|{tf}|{fam}|{direction}",
        "symbol": symbol, "timeframe": tf, "tf_label": TF_LABEL[tf],
        "family": fam, "family_label": FAMILIES[fam]["label"], "kind": kind,
        "pattern": name, "direction": direction, "status": raw["status"],
        "score": int(max(0, min(100, score))), "grade": _grade(score),
        "matrix_level": matrix_level,
        "ltp": P(fr.c[last]), "trigger": P(trig_level), "stop": P(stop), "target": P(target),
        "rr": round(rr, 2), "dist_atr": raw["dist_atr"], "atr": round(a, 2),
        "vol_ratio": round(vol_ratio, 2) if vol_ratio is not None else None, "vol_note": vol_note,
        "htf_trend": htf_trend, "htf_aligned": aligned, "bars": int(bars),
        "fit": round(max(0.0, min(1.0, raw["fit"])), 2),
        "bar_time": int(fr.t[last]),
        "breakout_time": int(fr.t[bi]) if bi is not None else None,
        "breakdown": {"timeframe": round(tf_pts, 1), "fit": round(fit_pts, 1), "volume": vol_pts,
                      "trend": trend_pts, "rr": round(rr_pts, 1), "length": len_pts},
        "candles": candles, "segments": segments, "points": points,
    }


def detect_all(symbol, frames: dict, tfs=None):
    """frames: {tf: Frame}. Returns list of packaged pattern hits."""
    out = []
    htf_map = {"week": ("week", 40), "day": ("week", 20), "4hour": ("day", 20), "60minute": ("day", 20)}
    for tf in (tfs or TIMEFRAMES):
        fr = frames.get(tf)
        if fr is None or fr.n < 40 or float(fr.atr[-1]) <= 0:
            continue
        hkey, hlen = htf_map[tf]
        htf = frames.get(hkey)
        trend = trend_of(htf, hlen)
        k = PIVOT_ORDER[tf]
        for oriented in (fr, mirror(fr)):
            piv = find_pivots(oriented.h, oriented.l, k)
            for fam, level_map in PATTERN_MATRIX.items():
                if tf not in level_map or fam in LINE_FAMILIES:
                    continue
                try:
                    raw = DETECTORS[fam](oriented, piv, tf)
                except Exception:  # noqa: BLE001 - one bad detector never kills the scan
                    log.exception("detector %s failed for %s %s", fam, symbol, tf)
                    raw = None
                if raw:
                    pk = _package(symbol, tf, oriented, raw, trend, level_map[tf])
                    if pk:
                        out.append(pk)
            if any(tf in PATTERN_MATRIX[f] for f in LINE_FAMILIES):
                try:
                    raw = detect_line_patterns(oriented, piv, tf)
                except Exception:  # noqa: BLE001
                    log.exception("line detector failed for %s %s", symbol, tf)
                    raw = None
                if raw and tf in PATTERN_MATRIX.get(raw["key"], {}):
                    pk = _package(symbol, tf, oriented, raw, trend,
                                  PATTERN_MATRIX[raw["key"]][tf])
                    if pk:
                        out.append(pk)
    # one hit per (tf, family): keep the best-scoring direction
    best = {}
    for r in out:
        key = (r["timeframe"], r["family"])
        if key not in best or r["score"] > best[key]["score"]:
            best[key] = r
    return list(best.values())



# ---------------------------------------------------------------------------
# Data fetch + scan job
# ---------------------------------------------------------------------------
def _resample(df, rule, **kw):
    return df.resample(rule, **kw).agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna()


def build_frames(daily: pd.DataFrame, hourly: pd.DataFrame) -> dict:
    frames = {}
    if daily is not None and not daily.empty:
        frames["day"] = make_frame(daily)
        frames["week"] = make_frame(_resample(daily, "W-FRI"))
    if hourly is not None and not hourly.empty:
        frames["60minute"] = make_frame(hourly)
        frames["4hour"] = make_frame(_resample(hourly, "4h", origin="start_day", offset="9h15min"))
    return {k: v for k, v in frames.items() if v is not None}


def _to_df(rows):
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df = df.rename(columns={"date": "timestamp"}).set_index("timestamp")
    return df[~df.index.duplicated(keep="last")].sort_index()


_state_lock = threading.Lock()
_state = {"running": False, "done": 0, "total": 0, "started_at": None, "finished_at": None,
          "error": None, "trigger": None}


def get_progress():
    with _state_lock:
        return dict(_state)


def load_results():
    try:
        with open(RESULTS_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {"results": [], "scanned_at": None, "symbols": 0, "errors": 0, "duration_s": None}


def _save_results(payload):
    tmp = RESULTS_FILE + ".tmp"
    d = os.path.dirname(RESULTS_FILE)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, separators=(",", ":"))
    os.replace(tmp, RESULTS_FILE)


def run_scan(kite, trigger="manual"):
    from . import scanner  # local import keeps this module testable offline

    with _state_lock:
        if _state["running"]:
            return False
        _state.update(running=True, done=0, total=0, error=None, trigger=trigger,
                      started_at=scanner.now_ist().isoformat(timespec="seconds"), finished_at=None)
    t0 = time.time()
    results, errors = [], 0
    try:
        symbols = list(scanner.get_fno_stock_list(kite))
        inst = scanner._load_instrument_map(kite)
        universe = [(s, inst.get(s)) for s in symbols if inst.get(s)]
        for name in INDEX_SYMBOLS:
            tok = scanner.get_index_token(kite, name)
            if tok:
                universe.append((name, tok))
        with _state_lock:
            _state["total"] = len(universe)
        throttle = 0.6 if scanner.is_market_open() else 0.35
        now = scanner.now_ist()
        for sym, tok in universe:
            try:
                daily = _to_df(scanner._fetch_historical_chunked(
                    kite, tok, now - dt.timedelta(days=1000), now, "day"))
                time.sleep(throttle)
                hourly = _to_df(scanner._fetch_historical_chunked(
                    kite, tok, now - dt.timedelta(days=150), now, "60minute"))
                time.sleep(throttle)
                frames = build_frames(daily, hourly)
                results.extend(detect_all(sym, frames))
            except Exception:  # noqa: BLE001
                errors += 1
                log.exception("pattern scan failed for %s", sym)
            with _state_lock:
                _state["done"] += 1
        results.sort(key=lambda r: (r["status"] != "BREAKOUT", -r["score"]))
        _save_results({
            "results": results, "scanned_at": scanner.now_ist().isoformat(timespec="seconds"),
            "symbols": len(universe), "errors": errors, "duration_s": round(time.time() - t0, 1),
            "trigger": trigger,
        })
        return True
    except Exception as exc:  # noqa: BLE001
        log.exception("pattern scan aborted")
        with _state_lock:
            _state["error"] = str(exc)
        return False
    finally:
        with _state_lock:
            _state["running"] = False
            _state["finished_at"] = scanner.now_ist().isoformat(timespec="seconds")


def start_scan_async(kite, trigger="manual"):
    if get_progress()["running"]:
        return False
    threading.Thread(target=run_scan, args=(kite, trigger), daemon=True).start()
    return True


# Auto-scan slots (IST): one midday refresh for 1H/4H setups, one after the close.
AUTO_SLOTS = [(12, 15), (15, 40)]
_scheduler_started = False


def _scheduler_loop():
    from . import kite_auth, research_runtime, scanner
    done_slots = set()
    while True:
        try:
            now = scanner.now_ist()
            if now.weekday() < 5:
                for hh, mm in AUTO_SLOTS:
                    key = (now.date(), hh, mm)
                    due = (now.hour, now.minute) >= (hh, mm)
                    if due and key not in done_slots:
                        kite = kite_auth.get_kite_client()
                        if kite is None or research_runtime.is_research_active():
                            continue
                        done_slots.add(key)
                        # skip earlier slots already superseded on a late boot
                        for h2, m2 in AUTO_SLOTS:
                            if (h2, m2) < (hh, mm):
                                done_slots.add((now.date(), h2, m2))
                        run_scan(kite, trigger=f"auto {hh:02d}:{mm:02d}")
        except Exception:  # noqa: BLE001
            log.exception("pattern scheduler iteration failed")
        time.sleep(60)


def start_scheduler_once():
    global _scheduler_started
    if _scheduler_started or os.getenv("PATTERN_AUTO_SCAN", "1") != "1" or os.getenv("PYTEST_CURRENT_TEST"):
        return
    _scheduler_started = True
    threading.Thread(target=_scheduler_loop, daemon=True).start()


def public_rows(payload):
    """Results without the heavy chart arrays, for the table."""
    heavy = {"candles", "segments", "points"}
    return [{k: v for k, v in r.items() if k not in heavy} for r in payload.get("results", [])]


def matrix_for_ui():
    return [
        {"family": fam, "label": FAMILIES[fam]["label"], "kind": FAMILIES[fam]["kind"],
         "levels": {tf: PATTERN_MATRIX[fam].get(tf) for tf in TIMEFRAMES}, "note": MATRIX_NOTES[fam]}
        for fam in PATTERN_MATRIX
    ]
