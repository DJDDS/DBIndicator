"""Fast Swing Chart Pattern Scanner.

Purpose: underlying-price swing discovery for the next 1-5 trading sessions.

Production pattern engine:
    Daily completed candles only.
Context:
    Last completed Weekly structure/trend.
Core structures:
    VCP/Tight Base, Rectangle/Flat Base, Flag/Pennant,
    Ascending/Descending Triangle, Double Top/Bottom,
    Three Rising Valleys/Falling Peaks.

Evidence is deliberately separated:
    1) Global research basis = descriptive provenance only (Direct / Mechanism / Limited).
    2) NSE evidence = this scanner's own D1-D5 forward outcomes, with sample maturity.

Neither global research labels nor NSE outcome percentages are added to the live
pattern-quality score. This module only spots and ranks patterns; it never places orders.
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
TIMEFRAMES = ["day"]
TF_LABEL = {"week": "Weekly", "day": "Daily", "4hour": "4H", "60minute": "1H"}
# Timeframe points (/20). Daily is the research anchor (deepest published evidence);
# Weekly is slower context for a 2-10 session swing; 4H is setup maturation; 1H is
# mainly an entry-refinement timeframe and does not inherit Daily statistics.
TF_POINTS = {"week": 18, "day": 20, "4hour": 15, "60minute": 10}
# Multi-scale pivot search. Daily is the only production pattern timeframe, but
# three swing sensitivities are evaluated so one arbitrary pivot order cannot
# decide whether a setup exists. The best-fitting candidate survives.
PIVOT_ORDERS = {"week": (3,), "day": (3, 5, 8), "4hour": (4,), "60minute": (5,)}
# Backward-compatible nominal pivot for any external diagnostics/tests.
PIVOT_ORDER = {tf: vals[len(vals) // 2] for tf, vals in PIVOT_ORDERS.items()}
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
    "three_valleys": {"label": "Three Rising Valleys / Falling Peaks", "kind": "reversal"},
    "vcp": {"label": "VCP / Tight Base", "kind": "continuation"},
}

# Which pattern family is searched on which timeframe.
# "primary" = most effective, "ok" = valid but weaker.
PATTERN_MATRIX = {
    # Production scope: fast swing structures only. Daily is the pattern engine;
    # Weekly is context and is never emitted as a separate setup.
    "double": {"day": "primary"},
    "three_valleys": {"day": "primary"},
    "vcp": {"day": "primary"},
    "rectangle": {"day": "primary"},
    "flag": {"day": "primary"},
    "triangle": {"day": "primary"},
}

# Evidence from the 26-Sep-2026 research note warrants collecting these two
# families, but not promoting them into the live production shortlist until our
# own D1-D5 NSE ledger has enough resolved events. They are recorded as shadow
# activations only and never appear in the live setup table.
RESEARCH_SHADOW_MATRIX = {
    "cup_handle": {"day": "shadow"},
    "wedge": {"day": "shadow"},
}

# Global research basis. These are provenance labels, NOT grades, win rates or score inputs.
# DIRECT = published work tests the named structure or a very close rule.
# MECHANISM = strong evidence exists for the underlying behaviour (momentum/level breakout/
# compression), but not enough clean evidence for the exact textbook name.
# LIMITED = direct peer-reviewed support is comparatively thin; NSE data must carry the weight.
RESEARCH_BASIS = {
    "flag": {
        "label": "DIRECT",
        "note": "Named flag-pattern studies across US/Europe/China; continuation mechanism also supported by momentum research.",
    },
    "rectangle": {
        "label": "DIRECT",
        "note": "Trading-range breakout and support/resistance research directly support level-break behaviour.",
    },
    "double": {
        "label": "DIRECT",
        "note": "Classical-pattern research includes double tops/bottoms; activation requires neckline confirmation.",
    },
    "vcp": {
        "label": "MECHANISM",
        "note": "Exact VCP evidence is limited; momentum + contraction + resistance-break mechanisms are well supported.",
    },
    "triangle": {
        "label": "MECHANISM",
        "note": "Repeated-level pressure and breakout mechanisms are supported; exact triangle labels are less directly studied.",
    },
    "three_valleys": {
        "label": "LIMITED",
        "note": "Useful progressive swing structure, but direct peer-reviewed evidence is thinner than for flags/range breakouts.",
    },
    "cup_handle": {
        "label": "RESEARCH",
        "note": "26-Sep-2026 NSE walk-forward study: strongest incremental bullish family versus shuffled null; shadow-only pending our D1-D5 validation.",
    },
    "wedge": {
        "label": "RESEARCH",
        "note": "26-Sep-2026 NSE walk-forward study: falling wedge showed borderline incremental structure versus shuffled null; shadow-only pending our D1-D5 validation.",
    },
}

NSE_EVIDENCE_EARLY = 30
NSE_EVIDENCE_MATURE = 100

# Production holding objective: fast underlying-price swing after confirmed activation.
HORIZON = {
    "flag": "1-5 trading days",
    "rectangle": "1-5 trading days",
    "triangle": "1-5 trading days",
    "vcp": "1-5 trading days",
    "double": "1-5 trading days",
    "three_valleys": "1-5 trading days",
    "cup_handle": "1-5 trading days (research shadow)",
    "wedge": "1-5 trading days (research shadow)",
}

MATRIX_NOTES = {
    "vcp": "Compression + shrinking pullbacks + prior impulse. Mechanism-supported; NSE D1-D5 evidence decides promotion.",
    "rectangle": "Objective flat-base / range pressure. Breakout through a repeatedly tested level.",
    "flag": "Real impulse first, then controlled pullback. No pole = no flag.",
    "triangle": "Directional pressure only: ascending / descending. Symmetrical triangles are excluded from production.",
    "double": "Confirmed only after neckline close. A W/M shape alone is not an activated setup.",
    "three_valleys": "Progressive swing structure: higher lows / lower highs, then boundary break.",
}

FAILED_LOOKBACK = 10      # breakout closed back inside within 10 bars = FAILED
PRIMARY_DAYS = 5          # production evaluation horizon: D1..D5 trading sessions
DIAGNOSTIC_DAYS = 10      # D10 is stored only as a diagnostic
FAST_DAYS = 2
SUCCESS_ATR, ADVERSE_ATR = 1.0, 0.75
FAST_SUCCESS_ATR, FAST_ADVERSE_ATR = 0.5, 0.5

FRESH_BARS = 3
RETEST_LOOKBACK = 5
RETEST_TOUCH_ATR = 0.25
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
FORWARD_FILE = os.getenv("PATTERN_FORWARD_FILE") or os.path.join(
    os.path.dirname(RESULTS_FILE) or ".", "pattern_forward_d1d5_ledger.json")
FORWARD_MAX_EVENTS = 5000


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
    if c[last] <= line(last):
        # Breakout closed back inside the structure.
        if last - j <= FAILED_LOOKBACK:
            return {"status": "FAILED", "dist_atr": round((line(last) - c[last]) / a, 2),
                    "break_i": j, "retest_i": None}
        return None

    # A recent pullback into the trigger that closes back on the breakout side is
    # a distinct swing state. This is deliberately price-only and symmetric under
    # mirroring, so bullish and bearish retests use identical rules.
    retest_i = None
    for i in range(j + 1, last + 1):
        if fr.l[i] <= line(i) + RETEST_TOUCH_ATR * a and fr.c[i] >= line(i):
            retest_i = i

    ext = (c[last] - line(last)) / a
    if last - j >= FRESH_BARS:
        if retest_i is not None and last - j <= RETEST_LOOKBACK and last - retest_i <= 2:
            return {"status": "EXTENDED" if ext > CHASE_ATR else "RETEST_HOLD",
                    "dist_atr": round(-ext, 2), "break_i": j, "retest_i": retest_i}
        return None
    return {"status": "EXTENDED" if ext > CHASE_ATR else "BREAKOUT",
            "dist_atr": round(-ext, 2), "break_i": j, "retest_i": retest_i}


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
    """Production line structures: rectangle and directional triangle only."""
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



def detect_falling_wedge(fr: Frame, piv, tf):
    """Research-only falling wedge in bullish orientation.

    Both fitted boundaries slope down and converge, with the upper boundary
    falling faster than the lower boundary. Mirroring yields the bearish rising
    wedge. This detector is never part of PATTERN_MATRIX; it only feeds the
    D1-D5 shadow ledger.
    """
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
        if up.resid > 0.7 * a or lo.resid > 0.7 * a:
            continue
        du, dl = up.slope * span / a, lo.slope * span / a
        w0, w1 = up(start) - lo(start), up(end) - lo(end)
        # Falling wedge: both lines fall; resistance falls faster; width contracts.
        if not (du < dl < -0.15 and w0 >= 2 * a and 0 < w1 / w0 < 0.75):
            continue
        xs = np.arange(start, end + 1)
        inside = fr.c[start:end + 1]
        outside = np.sum((inside > up(xs) + 0.35 * a) | (inside < lo(xs) - 0.35 * a))
        if outside > 2:
            continue
        stop = min(lo(fr.n - 1), ls[-1][1]) - 0.25 * a
        st = _status(fr, up, end + 1, stop)
        if not st:
            continue
        touches = len(hs) + len(ls)
        convergence = 1 - min(1.0, max(0.0, w1 / w0))
        fit = 0.5 * (1 - (up.resid + lo.resid) / (1.4 * a)) + 0.3 * min(1.0, (touches - 3) / 3) + 0.2 * convergence
        res = {
            "key": "wedge", "bull": "Falling Wedge", "bear": "Rising Wedge",
            "line": up, "stop": stop, "height": w0, "start_i": start,
            "fit": fit, **st,
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


def detect_three_valleys(fr: Frame, piv, tf):
    """Three rising valleys (bull) / three falling peaks (bear via mirror).
    Three swing lows, each clearly higher, breakout on a close above the
    highest peak between them (Bulkowski's confirmation)."""
    a = float(fr.atr[-1])
    cands = [p for p in piv if p[0] >= fr.n - LOOKBACK_BARS[tf]]
    for e in range(len(cands) - 1, 3, -1):
        seq = cands[e - 4:e + 1]
        if [p[2] for p in seq] != ["L", "H", "L", "H", "L"]:
            continue
        v1, h1, v2, h2, v3 = seq
        if not (v2[1] >= v1[1] + 0.3 * a and v3[1] >= v2[1] + 0.3 * a):
            continue
        sp1, sp2 = v2[0] - v1[0], v3[0] - v2[0]
        if sp1 < 5 or sp2 < 5 or v3[0] - v1[0] < MIN_BARS[tf]:
            continue
        top = max(h1[1], h2[1])
        height = top - v1[1]
        if height < 2 * a or v3[1] > top - 0.8 * a:
            continue
        if fr.l[v3[0]:].min() < v2[1]:
            continue   # structure broken: price undercut the middle valley
        pre = fr.h[max(0, v1[0] - (v3[0] - v1[0])):v1[0] + 1]
        if pre.size == 0 or pre.max() < top:
            continue   # a bottom reversal needs a prior decline from above the breakout level
        trig = Line(0, top, 1, top)
        stop = v3[1] - 0.25 * a
        st = _status(fr, trig, v3[0] + 1, stop)
        if not st:
            continue
        even = 1 - min(1.0, abs(math.log(sp2 / sp1)) / math.log(3))
        vol_ok = _mean(fr.v[v3[0] - 2:v3[0] + 3]) < _mean(fr.v[v1[0] - 2:v1[0] + 3])
        fit = 0.45 * even + 0.35 * min(1.0, (v3[1] - v1[1]) / (1.5 * a)) + (0.2 if vol_ok else 0)
        return {
            "key": "three_valleys", "bull": "Three Rising Valleys", "bear": "Three Falling Peaks",
            "line": trig, "stop": stop, "height": height, "start_i": v1[0], "fit": fit, **st,
            "points": [(v1[0], v1[1], "V1"), (v2[0], v2[1], "V2"), (v3[0], v3[1], "V3")],
            "segments": [[(p[0], p[1]) for p in seq]],
            "trigger_line": trig, "trigger_from": v1[0],
        }
    return None


def detect_vcp(fr: Frame, piv, tf):
    """Volatility contraction / tight base in bullish orientation.

    The mirrored frame applies the same mathematics to bearish compression:
    prior directional impulse, shrinking pullbacks, then boundary break.
    """
    a = float(fr.atr[-1])
    win = {"day": 80, "4hour": 100}.get(tf, 80)
    pv = [p for p in piv if p[0] >= fr.n - win]
    pairs = [(pv[i], pv[i + 1]) for i in range(len(pv) - 1) if pv[i][2] == "H" and pv[i + 1][2] == "L"]
    if len(pairs) < 2 or fr.n - 1 - pairs[-1][1][0] > 25:
        return None
    depths = [h[1] - l[1] for h, l in pairs]
    m = 1
    for k in range(len(depths) - 1, 0, -1):
        if depths[k] <= 0.85 * depths[k - 1]:
            m += 1
        else:
            break
    m = min(m, 4)
    if m < 2:
        return None
    use = pairs[-m:]
    d = depths[-m:]
    if d[-1] > 0.6 * d[0] or d[-1] > 3 * a or d[0] < 2 * a:
        return None
    first_h = use[0][0]
    if any(h[1] < first_h[1] - 1.0 * a for h, _ in use):
        return None   # descending highs = distribution, not a base
    pre_i = max(0, first_h[0] - 40)
    if fr.c[first_h[0]] - fr.c[pre_i] < 2 * a:
        return None   # needs a prior advance (impulse before the contraction)
    last_h, last_l = use[-1]
    top = last_h[1]
    trig = Line(0, top, 1, top)
    stop = last_l[1] - 0.25 * a
    st = _status(fr, trig, last_l[0] + 1, stop)
    if not st:
        return None
    vol_ok = _mean(fr.v[last_h[0]:last_l[0] + 1]) < _mean(fr.v[first_h[0]:use[0][1][0] + 1])
    fit = 0.4 * (1 - d[-1] / d[0]) + 0.3 * min(1.0, m / 3) + (0.3 if vol_ok else 0)
    pts = []
    for h, l in use:
        pts += [(h[0], h[1], ""), (l[0], l[1], "")]
    return {
        "key": "vcp", "bull": "VCP / Tight Base Breakout", "bear": "Tight Base Breakdown",
        "line": trig, "stop": stop, "height": d[0], "start_i": first_h[0], "fit": fit, **st,
        "points": pts, "segments": [[(i, p) for i, p, _ in pts]],
        "trigger_line": trig, "trigger_from": first_h[0],
    }


DETECTORS = {
    "head_shoulders": detect_inverse_hs,
    "double": lambda fr, pv, tf: detect_double_triple(fr, pv, tf, "double"),
    "triple": lambda fr, pv, tf: detect_double_triple(fr, pv, tf, "triple"),
    "cup_handle": detect_cup_handle,
    "flag": detect_flag,
    "three_valleys": detect_three_valleys,
    "vcp": detect_vcp,
}
LINE_FAMILIES = {"triangle", "rectangle"}


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


def _prior_trend(fr: Frame, start_i, height, kind):
    """ATR/height-normalised move INTO the pattern (oriented frame).
    Reversals need a prior move against the breakout; continuations need one with it."""
    w = 40 if start_i >= 40 else start_i
    if w < 5 or height <= 0:
        return 0.0
    move = fr.c[start_i] - fr.c[start_i - w]
    prior = -move if kind == "reversal" else move
    return float(prior / height)



def _volatility_regime(fr: Frame):
    """Current ATR% versus its trailing one-year (250-bar) median.

    This is descriptive/research-only: it does not alter the live quality score.
    """
    price = np.maximum(np.abs(fr.c), 1e-9)
    atr_pct = fr.atr / price
    tail = atr_pct[-250:] if len(atr_pct) >= 20 else atr_pct
    med = float(np.nanmedian(tail)) if len(tail) else 0.0
    now = float(atr_pct[-1]) if len(atr_pct) else 0.0
    ratio = now / med if med > 0 else None
    if ratio is None:
        label = "N/A"
    elif ratio < 0.85:
        label = "CALM"
    elif ratio <= 1.15:
        label = "NORMAL"
    else:
        label = "ELEVATED"
    return now * 100.0, ratio, label


def _breakout_thrust(fr: Frame, bi):
    """No-lookahead breakout-bar force, expressed as raw components.

    The combined score is for display/research only and carries zero live points.
    """
    if bi is None or bi < 0 or bi >= fr.n:
        return {"score": None, "close_location": None, "body_fraction": None, "range_atr": None}
    rng = float(fr.h[bi] - fr.l[bi])
    atr = float(fr.atr[bi])
    if rng <= 0 or atr <= 0:
        return {"score": None, "close_location": None, "body_fraction": None, "range_atr": None}
    close_location = float((fr.c[bi] - fr.l[bi]) / rng)
    body_fraction = float(abs(fr.c[bi] - fr.o[bi]) / rng)
    range_atr = float(rng / atr)
    score = (
        max(0.0, min(1.0, close_location)) +
        max(0.0, min(1.0, body_fraction)) +
        max(0.0, min(1.0, range_atr / 2.0))
    ) / 3.0
    return {
        "score": round(score, 3),
        "close_location": round(close_location, 3),
        "body_fraction": round(body_fraction, 3),
        "range_atr": round(range_atr, 3),
    }


def _candle_context(fr: Frame, raw):
    """Display-only candle names near the structural pivot and at breakout.

    Reuses the app's existing no-lookahead candle engine. No candle result
    contributes to live pattern score or eligibility.
    """
    try:
        from . import indicators
        idx = pd.to_datetime(fr.t, unit="s", utc=True).tz_convert("Asia/Kolkata")
        if fr.sign == 1:
            o, h, l, c = fr.o, fr.h, fr.l, fr.c
        else:
            # Convert the mirrored bearish search frame back to real market
            # prices before naming candles, so the UI never calls a real
            # bearish engulfing candle "Bullish Engulfing".
            o, h, l, c = -fr.o, -fr.l, -fr.h, -fr.c
        df = pd.DataFrame(
            {"open": o, "high": h, "low": l, "close": c, "volume": fr.v},
            index=idx,
        )
        direction, name = indicators._compute_candle_pattern(df)
    except Exception:
        return {"pivot": None, "pivot_direction": None, "breakout": None, "breakout_direction": None}

    points = raw.get("points") or []
    pivot_i = max((int(p[0]) for p in points), default=int(raw.get("start_i") or 0))
    candidates = []
    for i in range(max(0, pivot_i - 2), min(fr.n, pivot_i + 3)):
        nm = name.iloc[i]
        if nm:
            candidates.append((abs(i - pivot_i), i, nm, direction.iloc[i]))
    candidates.sort(key=lambda x: (x[0], x[1]))
    pivot = candidates[0] if candidates else None
    bi = raw.get("break_i")
    breakout_name = name.iloc[bi] if bi is not None and 0 <= bi < fr.n else None
    breakout_dir = direction.iloc[bi] if bi is not None and 0 <= bi < fr.n else None
    return {
        "pivot": pivot[2] if pivot else None,
        "pivot_direction": pivot[3] if pivot else None,
        "breakout": breakout_name,
        "breakout_direction": breakout_dir,
    }


def _package(symbol, tf, fr: Frame, raw, htf_trend, matrix_level):
    s = fr.sign
    bull = s == 1
    a = float(fr.atr[-1])
    last = fr.n - 1
    line = raw["line"]
    bi = raw.get("break_i")
    status = raw["status"]
    trig_level = line(bi) if bi is not None else line(last)
    entry = fr.c[last] if status in ("BREAKOUT", "EXTENDED") else trig_level
    target = trig_level + raw["height"]
    stop = raw["stop"]
    risk = entry - stop
    rr = (target - entry) / risk if risk > 0 else 0.0
    if rr <= 0 and status != "FAILED":
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
    name = raw["bull"] if bull else raw["bear"]
    aligned = (htf_trend == "UP" and bull) or (htf_trend == "DOWN" and not bull)
    opposed = (htf_trend == "UP" and not bull) or (htf_trend == "DOWN" and bull)
    research = RESEARCH_BASIS.get(fam, {"label": "LIMITED", "note": "NSE validation required."})
    height_atr = float(raw["height"] / a) if a > 0 else None
    atr_pct, regime_ratio, regime_label = _volatility_regime(fr)
    thrust = _breakout_thrust(fr, bi)
    candle_ctx = _candle_context(fr, raw)
    pivot_k = int(raw.get("pivot_k") or PIVOT_ORDER.get(tf, 0))

    # Global research basis, candles and research features never manufacture the
    # live score. They are captured so our own D1-D5 outcomes can validate them.
    # Quality is based only on the current underlying-price structure and context.
    fit_pts = 25 * max(0.0, min(1.0, raw["fit"]))
    if vol_ratio is None:
        vol_pts, vol_note = 8, "n/a"
    elif bi is not None and status != "FAILED":
        vol_pts = 15 if vol_ratio >= 1.5 else 9 if vol_ratio >= 1.2 else 3
        vol_note = "confirmed" if vol_ratio >= 1.5 else "moderate" if vol_ratio >= 1.2 else "unconfirmed"
    else:
        vol_pts = 15 if vol_ratio <= 0.8 else 9 if vol_ratio <= 1.0 else 3
        vol_note = "drying up" if vol_ratio <= 0.8 else "normal" if vol_ratio <= 1.0 else "elevated"

    if kind == "continuation":
        trend_pts = 15 if aligned else 0 if opposed else 7
    else:
        trend_pts = 15 if aligned else 5 if opposed else 10

    prior = _prior_trend(fr, raw["start_i"], raw["height"], kind)
    prior_pts = 15 if prior >= 1.0 else 9 if prior >= 0.5 else 4 if prior > 0 else 0
    state_pts = {"RETEST_HOLD": 10, "BREAKOUT": 8, "FORMING": 5, "EXTENDED": 2, "FAILED": 0}.get(status, 0)
    rr_pts = 10 * min(max(rr, 0.0) / 3.0, 1.0)
    bars = last - raw["start_i"]
    len_pts = 10 if bars >= MIN_BARS[tf] * 1.5 else 6
    score = int(max(0, min(100, round(
        fit_pts + vol_pts + trend_pts + prior_pts + state_pts + rr_pts + len_pts
    ))))
    grade = _grade(score)
    caps = []

    def cap(to, why):
        nonlocal grade
        if grade < to:
            grade = to
        caps.append(why)

    if status == "FORMING" and grade == "A":
        cap("B", "forming - activation requires a breakout close")
    if status == "EXTENDED" and grade == "A":
        cap("B", "extended - chase risk")
    if status == "FAILED":
        cap("C", "failed breakout - trap / invalidated")

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

    return {
        "id": f"{symbol}|{tf}|{fam}|{direction}",
        "symbol": symbol, "timeframe": tf, "tf_label": TF_LABEL[tf],
        "family": fam, "family_label": FAMILIES[fam]["label"], "kind": kind,
        "pattern": name, "direction": direction, "status": status,
        "score": score, "grade": grade, "grade_caps": caps,
        "research_basis": research["label"], "research_note": research["note"],
        "horizon": HORIZON.get(fam, ""),
        "prior_trend": round(prior, 2),
        "matrix_level": matrix_level,
        "ltp": P(fr.c[last]), "trigger": P(trig_level), "stop": P(stop), "target": P(target),
        "rr": round(rr, 2), "dist_atr": raw["dist_atr"], "atr": round(a, 2),
        "vol_ratio": round(vol_ratio, 2) if vol_ratio is not None else None, "vol_note": vol_note,
        "htf_trend": htf_trend, "htf_aligned": aligned, "bars": int(bars),
        "formation_sessions": int(bars + 1), "pattern_timeframe": TF_LABEL[tf],
        "fit": round(max(0.0, min(1.0, raw["fit"])), 2),
        "pivot_k": pivot_k,
        "height_atr": round(height_atr, 3) if height_atr is not None else None,
        "atr_pct": round(atr_pct, 3),
        "volatility_regime_ratio": round(regime_ratio, 3) if regime_ratio is not None else None,
        "volatility_regime": regime_label,
        "breakout_thrust": thrust["score"],
        "breakout_close_location": thrust["close_location"],
        "breakout_body_fraction": thrust["body_fraction"],
        "breakout_range_atr": thrust["range_atr"],
        "candle_pivot": candle_ctx["pivot"],
        "candle_pivot_direction": candle_ctx["pivot_direction"],
        "candle_breakout": candle_ctx["breakout"],
        "candle_breakout_direction": candle_ctx["breakout_direction"],
        "candle_weight": 0.0,
        "research_only": matrix_level == "shadow",
        "formation_start_time": int(fr.t[raw["start_i"]]),
        "bar_time": int(fr.t[last]),
        "breakout_time": int(fr.t[bi]) if bi is not None else None,
        "retest_time": int(fr.t[raw["retest_i"]]) if raw.get("retest_i") is not None else None,
        "breakout_close": P(fr.c[bi]) if bi is not None else None,
        "breakout_atr": round(float(fr.atr[bi]), 4) if bi is not None else None,
        "breakdown": {"fit": round(fit_pts, 1), "volume": vol_pts, "trend": trend_pts,
                      "prior": prior_pts, "state": state_pts, "rr": round(rr_pts, 1),
                      "length": len_pts},
        "candles": candles, "segments": segments, "points": points,
    }


def _detect_matrix(symbol, frames: dict, matrix: dict, tfs=None):
    """Run one pattern matrix across every configured pivot scale."""
    out = []
    htf_map = {"week": ("week", 40), "day": ("week", 20), "4hour": ("day", 20), "60minute": ("day", 20)}
    for tf in (tfs or TIMEFRAMES):
        fr = frames.get(tf)
        if fr is None or fr.n < 40 or float(fr.atr[-1]) <= 0:
            continue
        hkey, hlen = htf_map[tf]
        htf = frames.get(hkey)
        trend = trend_of(htf, hlen)
        for oriented in (fr, mirror(fr)):
            for k in PIVOT_ORDERS.get(tf, (PIVOT_ORDER[tf],)):
                piv = find_pivots(oriented.h, oriented.l, k)
                for fam, level_map in matrix.items():
                    if tf not in level_map:
                        continue
                    raw = None
                    try:
                        if fam in LINE_FAMILIES:
                            candidate = detect_line_patterns(oriented, piv, tf)
                            if candidate and candidate.get("key") == fam:
                                raw = candidate
                        elif fam == "wedge":
                            raw = detect_falling_wedge(oriented, piv, tf)
                        else:
                            raw = DETECTORS[fam](oriented, piv, tf)
                    except Exception:  # noqa: BLE001 - one detector/scale never kills scan
                        log.exception("detector %s failed for %s %s k=%s", fam, symbol, tf, k)
                    if raw:
                        raw["pivot_k"] = k
                        pk = _package(symbol, tf, oriented, raw, trend, level_map[tf])
                        if pk:
                            out.append(pk)

    # First choose best structural fit across pivot scales for each direction.
    by_direction = {}
    for r in out:
        key = (r["timeframe"], r["family"], r["direction"])
        if key not in by_direction or (r.get("fit", 0), r.get("score", 0)) > (
            by_direction[key].get("fit", 0), by_direction[key].get("score", 0)
        ):
            by_direction[key] = r

    # Preserve historical UI behaviour: one live row per (timeframe, family),
    # taking the stronger direction after scale selection.
    best = {}
    for r in by_direction.values():
        key = (r["timeframe"], r["family"])
        if key not in best or (r["score"], r.get("fit", 0)) > (
            best[key]["score"], best[key].get("fit", 0)
        ):
            best[key] = r
    return list(best.values())


def detect_all(symbol, frames: dict, tfs=None):
    """Production patterns only; research-shadow families are excluded."""
    return _detect_matrix(symbol, frames, PATTERN_MATRIX, tfs=tfs)


def detect_research_shadow(symbol, frames: dict, tfs=None):
    """Cup/handle and wedge research candidates; never live shortlist rows."""
    return _detect_matrix(symbol, frames, RESEARCH_SHADOW_MATRIX, tfs=tfs)



# ---------------------------------------------------------------------------
# Data fetch + scan job
# ---------------------------------------------------------------------------
def _resample(df, rule, **kw):
    return df.resample(rule, **kw).agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna()


def build_frames(daily: pd.DataFrame, hourly: pd.DataFrame | None = None) -> dict:
    """Daily pattern frame + last fully completed Weekly context only."""
    frames = {}
    if daily is not None and not daily.empty:
        frames["day"] = make_frame(daily)
        weekly = _resample(daily, "W-FRI")
        # W-FRI labels an incomplete Mon-Thu week with the upcoming Friday.
        if not weekly.empty and weekly.index[-1].date() > daily.index[-1].date():
            weekly = weekly.iloc[:-1]
        frames["week"] = make_frame(weekly)
    return {k: v for k, v in frames.items() if v is not None}


def _to_df(rows):
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df = df.rename(columns={"date": "timestamp"}).set_index("timestamp")
    return df[~df.index.duplicated(keep="last")].sort_index()


def _completed_daily(df: pd.DataFrame, now, market_open: bool) -> pd.DataFrame:
    """Never let an unfinished Daily candle create or destroy a swing pattern."""
    if df is None or df.empty:
        return df
    out = df
    if market_open and out.index[-1].date() == now.date():
        out = out.iloc[:-1]
    return out


# ---------------------------------------------------------------------------
# Forward validation ledger - builds NSE-specific evidence from live breakouts.
# Only ACTIVATED patterns (breakout close) are recorded; detection alone is not
# a signal. Each event is re-evaluated from raw candles on every scan, so the
# result never depends on stale intermediate state.
# ---------------------------------------------------------------------------
def load_forward(path=None):
    try:
        with open(path or FORWARD_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_forward(ledger, path=None):
    path = path or FORWARD_FILE
    if len(ledger) > FORWARD_MAX_EVENTS:
        keep = sorted(ledger.values(), key=lambda e: e.get("breakout_time") or 0)[-FORWARD_MAX_EVENTS:]
        ledger = {e["key"]: e for e in keep}
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(ledger, fh, separators=(",", ":"))
    os.replace(tmp, path)


def record_breakouts(ledger, rows, now_iso=None):
    """Record each confirmed activation once. The executable entry is next-session open."""
    added = 0
    for r in rows:
        if r.get("status") not in ("BREAKOUT", "RETEST_HOLD", "EXTENDED") or not r.get("breakout_time"):
            continue
        key = f"{r['id']}|{r['breakout_time']}"
        if key in ledger:
            # Preserve the strongest lifecycle information without duplicating the episode.
            if r.get("status") == "RETEST_HOLD":
                ledger[key]["latest_state"] = "RETEST_HOLD"
                ledger[key]["retest_time"] = r.get("retest_time")
            continue
        ledger[key] = {
            "key": key, "id": r["id"], "symbol": r["symbol"], "timeframe": r["timeframe"],
            "tf_label": r["tf_label"], "family": r["family"], "pattern": r["pattern"],
            "direction": r["direction"], "research_basis": r.get("research_basis"), "score": r["score"],
            "grade": r["grade"], "breakout_time": r["breakout_time"],
            "activation_state": r.get("status"), "latest_state": r.get("status"),
            "retest_time": r.get("retest_time"), "trigger": r["trigger"],
            "stop": r["stop"], "target": r["target"], "recorded_at": now_iso,
            "research_only": bool(r.get("research_only")),
            "pivot_k": r.get("pivot_k"), "height_atr": r.get("height_atr"),
            "vol_ratio": r.get("vol_ratio"),
            "volatility_regime_ratio": r.get("volatility_regime_ratio"),
            "volatility_regime": r.get("volatility_regime"),
            "breakout_thrust": r.get("breakout_thrust"),
            "breakout_close_location": r.get("breakout_close_location"),
            "breakout_body_fraction": r.get("breakout_body_fraction"),
            "breakout_range_atr": r.get("breakout_range_atr"),
            "candle_pivot": r.get("candle_pivot"),
            "candle_breakout": r.get("candle_breakout"),
            "candle_weight": 0.0,
            "outcome": "OPEN", "fast_outcome": "OPEN", "bars_seen": 0,
        }
        added += 1
    return added


def evaluate_event(ev, fr: Frame):
    """Evaluate one activation on a common Daily clock.

    Entry is the next trading session open after activation. D1..D5 are cumulative
    from that executable entry. D10 is retained only as a diagnostic.
    """
    if fr is None or not ev.get("breakout_time"):
        return ev
    idx = int(np.searchsorted(fr.t, int(ev["breakout_time"]), side="right"))
    if idx >= fr.n:
        return ev

    bull = ev["direction"] == "BULL"
    entry = float(fr.o[idx])
    atr_i = max(0, idx - 1)
    atr = float(fr.atr[atr_i])
    if entry <= 0 or atr <= 0:
        return ev

    end = min(fr.n - 1, idx + DIAGNOSTIC_DAYS - 1)
    mfe = mae = 0.0
    outcome, outcome_day = "OPEN", None
    fast_outcome, fast_day = "OPEN", None
    target_hit_day = stop_hit_day = None
    time_to_half = time_to_one = None
    metrics = {}

    for i in range(idx, end + 1):
        day = i - idx + 1
        fav = (fr.h[i] - entry) if bull else (entry - fr.l[i])
        adv = (entry - fr.l[i]) if bull else (fr.h[i] - entry)
        mfe = max(mfe, fav / atr)
        mae = max(mae, adv / atr)

        if time_to_half is None and mfe >= FAST_SUCCESS_ATR:
            time_to_half = day
        if time_to_one is None and mfe >= SUCCESS_ATR:
            time_to_one = day

        if day <= FAST_DAYS and fast_outcome == "OPEN":
            if adv >= FAST_ADVERSE_ATR * atr:
                fast_outcome, fast_day = "FAIL", day
            elif fav >= FAST_SUCCESS_ATR * atr:
                fast_outcome, fast_day = "SUCCESS", day

        if day <= PRIMARY_DAYS and outcome == "OPEN":
            if adv >= ADVERSE_ATR * atr:  # conservative when both thresholds occur in one candle
                outcome, outcome_day = "FAIL", day
            elif fav >= SUCCESS_ATR * atr:
                outcome, outcome_day = "SUCCESS", day

        hit_stop = fr.l[i] <= ev["stop"] if bull else fr.h[i] >= ev["stop"]
        hit_tgt = fr.h[i] >= ev["target"] if bull else fr.l[i] <= ev["target"]
        if stop_hit_day is None and hit_stop:
            stop_hit_day = day
        if target_hit_day is None and hit_tgt and stop_hit_day is None:
            target_hit_day = day

        ret = (fr.c[i] - entry) / entry * 100 * (1 if bull else -1)
        be = mfe / (mfe + mae) if (mfe + mae) > 0 else None
        if day <= PRIMARY_DAYS:
            metrics[f"ret_d{day}"] = round(ret, 2)
            metrics[f"mfe_d{day}"] = round(mfe, 2)
            metrics[f"mae_d{day}"] = round(mae, 2)
            metrics[f"be_d{day}"] = round(be, 3) if be is not None else None
        if day == DIAGNOSTIC_DAYS:
            metrics["diag_ret_d10"] = round(ret, 2)
            metrics["diag_mfe_d10"] = round(mfe, 2)
            metrics["diag_mae_d10"] = round(mae, 2)

    seen = end - idx + 1
    if fast_outcome == "OPEN" and seen >= FAST_DAYS:
        fast_outcome = "TIMEOUT"
    if outcome == "OPEN" and seen >= PRIMARY_DAYS:
        outcome = "TIMEOUT"

    ev.update(
        entry_time=int(fr.t[idx]), entry=round(entry, 2), atr=round(atr, 4),
        bars_seen=int(seen), mfe_atr=round(mfe, 2), mae_atr=round(mae, 2),
        outcome=outcome, outcome_day=outcome_day,
        fast_outcome=fast_outcome, fast_day=fast_day,
        time_to_half_atr=time_to_half, time_to_one_atr=time_to_one,
        target_hit_day=target_hit_day, stop_hit_day=stop_hit_day, **metrics
    )
    return ev


def update_forward(ledger, symbol, frames):
    daily = frames.get("day")
    for ev in ledger.values():
        if ev.get("symbol") == symbol and ev.get("bars_seen", 0) < DIAGNOSTIC_DAYS:
            try:
                evaluate_event(ev, daily)
            except Exception:  # noqa: BLE001
                log.exception("D1-D5 forward evaluation failed for %s", ev.get("key"))


def forward_summary(ledger=None):
    """Pattern x direction summary on the common D1-D5 Daily clock."""
    ledger = load_forward() if ledger is None else ledger
    groups = {}
    for ev in ledger.values():
        groups.setdefault((ev["pattern"], ev["direction"]), []).append(ev)

    def median(key, src):
        vals = [float(e[key]) for e in src if e.get(key) is not None]
        return round(float(np.median(vals)), 2) if vals else None

    rows = []
    for (pattern, direction), evs in groups.items():
        done = [e for e in evs if e.get("outcome") in ("SUCCESS", "FAIL", "TIMEOUT")]
        fast_done = [e for e in evs if e.get("fast_outcome") in ("SUCCESS", "FAIL", "TIMEOUT")]
        wins = sum(1 for e in done if e["outcome"] == "SUCCESS")
        fast_wins = sum(1 for e in fast_done if e["fast_outcome"] == "SUCCESS")
        row = {
            "pattern": pattern, "direction": direction, "tf_label": "Daily",
            "research_only": any(bool(e.get("research_only")) for e in evs),
            "events": len(evs), "resolved": len(done),
            "success_pct": round(100 * wins / len(done), 1) if done else None,
            "fast_success_pct": round(100 * fast_wins / len(fast_done), 1) if fast_done else None,
            "median_mae_d5": median("mae_d5", evs),
            "median_ret_d5": median("ret_d5", evs),
        }
        for d in range(1, PRIMARY_DAYS + 1):
            row[f"median_mfe_d{d}"] = median(f"mfe_d{d}", evs)
        rows.append(row)

    rows.sort(key=lambda r: (-r["resolved"], -r["events"], r["pattern"]))
    total = [e for e in ledger.values() if e.get("outcome") in ("SUCCESS", "FAIL", "TIMEOUT")]
    fast_total = [e for e in ledger.values() if e.get("fast_outcome") in ("SUCCESS", "FAIL", "TIMEOUT")]
    wins = sum(1 for e in total if e["outcome"] == "SUCCESS")
    fast_wins = sum(1 for e in fast_total if e["fast_outcome"] == "SUCCESS")
    return {
        "rows": rows, "events": len(ledger), "resolved": len(total),
        "success_pct": round(100 * wins / len(total), 1) if total else None,
        "fast_success_pct": round(100 * fast_wins / len(fast_total), 1) if fast_total else None,
        "rule": "+1 ATR before -0.75 ATR by D5; fast = +0.5 ATR before -0.5 ATR by D2; next-session-open entry",
    }


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
    from . import research_runtime, scanner  # local imports keep module testable offline

    # Pattern scans are heavy Kite-history users. Share the same exclusive slot
    # as live/research jobs so the 15:45 scan cannot collide with V12/research.
    if not research_runtime.live_scan_slot():
        with _state_lock:
            _state["error"] = "heavy scanner/research slot busy - will retry"
        return False

    with _state_lock:
        if _state["running"]:
            research_runtime.exit_live_scan()
            return False
        _state.update(running=True, done=0, total=0, error=None, trigger=trigger,
                      started_at=scanner.now_ist().isoformat(timespec="seconds"), finished_at=None)
    t0 = time.time()
    results, errors = [], 0
    ledger = load_forward()
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
        market_open = scanner.is_market_open()
        throttle = 0.6 if market_open else 0.35
        now = scanner.now_ist()
        for sym, tok in universe:
            try:
                daily = _to_df(scanner._fetch_historical_chunked(
                    kite, tok, now - dt.timedelta(days=1000), now, "day"))
                daily = _completed_daily(daily, now, market_open)
                time.sleep(throttle)
                frames = build_frames(daily)
                hits = detect_all(sym, frames)
                shadow_hits = detect_research_shadow(sym, frames)
                results.extend(hits)
                record_breakouts(ledger, hits, now.isoformat(timespec="seconds"))
                record_breakouts(ledger, shadow_hits, now.isoformat(timespec="seconds"))
                update_forward(ledger, sym, frames)
            except Exception:  # noqa: BLE001
                errors += 1
                log.exception("pattern scan failed for %s", sym)
            with _state_lock:
                _state["done"] += 1
        status_rank = {"RETEST_HOLD": 0, "BREAKOUT": 1, "FORMING": 2, "EXTENDED": 3, "FAILED": 4}
        results.sort(key=lambda r: (status_rank.get(r["status"], 9), -r["score"]))
        try:
            save_forward(ledger)
        except OSError:
            log.exception("could not save pattern forward ledger")
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
        research_runtime.exit_live_scan()
        with _state_lock:
            _state["running"] = False
            _state["finished_at"] = scanner.now_ist().isoformat(timespec="seconds")


def start_scan_async(kite, trigger="manual"):
    if get_progress()["running"]:
        return False
    threading.Thread(target=run_scan, args=(kite, trigger), daemon=True).start()
    return True


# Daily swing engine: one scan after the NSE cash close. Manual intraday scans use
# the last completed Daily candle, so an unfinished candle can never repaint a setup.
# 15:45 IST deliberately sits after the V12 POST_CAS derivative recorder window
# (which runs through 15:40) and after the Daily cash candle is final.
AUTO_SLOTS = [(15, 45)]
_scheduler_started = False


def _scheduler_loop():
    from . import kite_auth, research_runtime, scanner
    done_slots = set()
    active_date = None
    while True:
        try:
            now = scanner.now_ist()
            if active_date != now.date():
                done_slots.clear()
                active_date = now.date()
            if now.weekday() < 5:
                for hh, mm in AUTO_SLOTS:
                    key = (now.date(), hh, mm)
                    due = (now.hour, now.minute) >= (hh, mm)
                    if due and key not in done_slots:
                        kite = kite_auth.get_kite_client()
                        if kite is None or research_runtime.is_research_active():
                            continue
                        # Mark complete only after a successful scan. A busy heavy
                        # slot or transient abort therefore retries next minute.
                        ok = run_scan(kite, trigger=f"auto {hh:02d}:{mm:02d}")
                        if ok:
                            done_slots.add(key)
                            # skip earlier slots already superseded on a late boot
                            for h2, m2 in AUTO_SLOTS:
                                if (h2, m2) < (hh, mm):
                                    done_slots.add((now.date(), h2, m2))
        except Exception:  # noqa: BLE001
            log.exception("pattern scheduler iteration failed")
        time.sleep(60)


def start_scheduler_once():
    global _scheduler_started
    if _scheduler_started or os.getenv("PATTERN_AUTO_SCAN", "1") != "1" or os.getenv("PYTEST_CURRENT_TEST"):
        return
    _scheduler_started = True
    threading.Thread(target=_scheduler_loop, daemon=True).start()


def _nse_evidence_map(ledger=None):
    """Current NSE D1-D5 evidence keyed by (pattern, direction)."""
    summary = forward_summary(load_forward() if ledger is None else ledger)
    out = {}
    for row in summary["rows"]:
        resolved = int(row.get("resolved") or 0)
        if resolved < NSE_EVIDENCE_EARLY:
            stage = "BUILDING"
        elif resolved < NSE_EVIDENCE_MATURE:
            stage = "EARLY"
        else:
            stage = "MATURE"
        out[(row["pattern"], row["direction"])] = {
            "stage": stage,
            "events": int(row.get("events") or 0),
            "resolved": resolved,
            "fast_success_pct": row.get("fast_success_pct"),
            "swing_success_pct": row.get("success_pct"),
            "median_ret_d5": row.get("median_ret_d5"),
            "median_mae_d5": row.get("median_mae_d5"),
        }
    return out


def nse_evidence_for(pattern, direction, ledger=None):
    return _nse_evidence_map(ledger).get((pattern, direction), {
        "stage": "BUILDING", "events": 0, "resolved": 0,
        "fast_success_pct": None, "swing_success_pct": None,
        "median_ret_d5": None, "median_mae_d5": None,
    })


def public_rows(payload, ledger=None):
    """Results without chart payload, enriched with our own NSE evidence."""
    heavy = {"candles", "segments", "points"}
    nse = _nse_evidence_map(ledger)
    rows = []
    for r in payload.get("results", []):
        row = {k: v for k, v in r.items() if k not in heavy}
        row["nse_evidence"] = nse.get((r.get("pattern"), r.get("direction"))) or nse_evidence_for(
            r.get("pattern"), r.get("direction"), ledger
        )
        rows.append(row)
    return rows


def matrix_for_ui():
    return [
        {"family": fam, "label": FAMILIES[fam]["label"], "kind": FAMILIES[fam]["kind"],
         "levels": {tf: PATTERN_MATRIX[fam].get(tf) for tf in TIMEFRAMES},
         "note": MATRIX_NOTES[fam],
         "research_basis": RESEARCH_BASIS[fam]["label"],
         "research_note": RESEARCH_BASIS[fam]["note"],
         "horizon": HORIZON.get(fam, "")}
        for fam in PATTERN_MATRIX
    ]
