"""One-week order-flow recorder (research only, never trades).

For every cash-equity tick already streaming into the tactical socket, this
keeps a per-symbol, per-minute summary and appends one JSONL row when the
minute closes:

* book state at the minute's last tick (exact, from Kite 5-level depth):
  L1 and weighted L5 imbalance, total buy/sell quantity imbalance, spread,
  microprice bias;
* approximate aggressor delta over the minute (Kite FULL ticks arrive about
  once per second, so volume between snapshots is classified by quote rule:
  trade at/above previous ask = buy, at/below previous bid = sell, otherwise
  tick rule vs previous price).

Forward returns are NOT computed live; the evaluation script joins the next
rows' close prices, so the recorder cannot leak the future into a feature.
"""
from __future__ import annotations

import datetime as dt
import json
import threading
from pathlib import Path

SESSION_START = dt.time(9, 15)
SESSION_END = dt.time(15, 30)
SCHEMA = "v124_orderflow_minute_v1"


def _f(value):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None


def book_features(tick: dict) -> dict:
    depth = (tick or {}).get("depth") or {}
    buys = [x for x in list(depth.get("buy") or [])[:5] if isinstance(x, dict)]
    sells = [x for x in list(depth.get("sell") or [])[:5] if isinstance(x, dict)]

    def q(level):
        return max(0.0, _f(level.get("quantity")) or 0.0)

    def p(level):
        v = _f(level.get("price"))
        return v if v and v > 0 else None

    bid = p(buys[0]) if buys else None
    ask = p(sells[0]) if sells else None
    bidq = q(buys[0]) if buys else 0.0
    askq = q(sells[0]) if sells else 0.0
    weights = (1.0, 0.75, 0.55, 0.40, 0.30)
    wb = sum(weights[i] * q(x) for i, x in enumerate(buys))
    ws = sum(weights[i] * q(x) for i, x in enumerate(sells))
    tbq = _f(tick.get("total_buy_quantity")) or 0.0
    tsq = _f(tick.get("total_sell_quantity")) or 0.0
    out = {
        "bid": bid,
        "ask": ask,
        "l1_imb": (bidq - askq) / (bidq + askq) if bidq + askq > 0 else None,
        "l5_imb": (wb - ws) / (wb + ws) if wb + ws > 0 else None,
        "tot_imb": (tbq - tsq) / (tbq + tsq) if tbq + tsq > 0 else None,
        "spread_bps": None,
        "micro_bps": None,
    }
    if bid and ask and ask >= bid:
        mid = (bid + ask) / 2.0
        out["spread_bps"] = 1e4 * (ask - bid) / mid
        if bidq + askq > 0:
            micro = (bid * askq + ask * bidq) / (bidq + askq)
            out["micro_bps"] = 1e4 * (micro - mid) / mid
    return out


class OrderFlowMinuteRecorder:
    def __init__(self, path):
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self._state: dict[str, dict] = {}
        self.rows_written = 0
        self.last_error = None

    def reset(self):
        with self._lock:
            self._state = {}

    def on_cash_tick(self, symbol: str, tick: dict, now: dt.datetime):
        if not self.path or not symbol or now is None:
            return
        t = now.time()
        if t < SESSION_START or t >= SESSION_END:
            return
        px = _f(tick.get("last_price"))
        vol = _f(tick.get("volume_traded", tick.get("volume")))
        if not px or px <= 0:
            return
        minute = now.replace(second=0, microsecond=0)
        book = book_features(tick)
        emit = None
        with self._lock:
            st = self._state.get(symbol)
            if st is not None and st["minute"] != minute:
                emit = self._row(symbol, st)
                st = None
            if st is None:
                prev = self._state.get(symbol) or {}
                st = {
                    "minute": minute,
                    "open": px,
                    "buy": 0.0,
                    "sell": 0.0,
                    "unk": 0.0,
                    "ticks": 0,
                    "prev_px": prev.get("last_px"),
                    "prev_vol": prev.get("last_vol"),
                    "prev_bid": prev.get("last_bid"),
                    "prev_ask": prev.get("last_ask"),
                    "imb_sum": 0.0,
                    "imb_n": 0,
                }
                self._state[symbol] = st
            dv = None
            if vol is not None and st.get("prev_vol") is not None:
                dv = max(0.0, vol - st["prev_vol"])
            if dv:
                pb, pa, pp = st.get("prev_bid"), st.get("prev_ask"), st.get("prev_px")
                if pa and px >= pa:
                    st["buy"] += dv
                elif pb and px <= pb:
                    st["sell"] += dv
                elif pp and px > pp:
                    st["buy"] += dv
                elif pp and px < pp:
                    st["sell"] += dv
                else:
                    st["unk"] += dv
            if book["l5_imb"] is not None:
                st["imb_sum"] += book["l5_imb"]
                st["imb_n"] += 1
            st["ticks"] += 1
            st["close"] = px
            st["book"] = book
            st["prev_px"] = st["last_px"] = px
            if vol is not None:
                st["prev_vol"] = st["last_vol"] = vol
            st["prev_bid"] = st["last_bid"] = book["bid"]
            st["prev_ask"] = st["last_ask"] = book["ask"]
        if emit:
            self._write(emit)

    @staticmethod
    def _row(symbol, st):
        book = st.get("book") or {}
        total = st["buy"] + st["sell"]
        return {
            "schema": SCHEMA,
            "minute": st["minute"].isoformat(),
            "symbol": symbol,
            "open": st.get("open"),
            "close": st.get("close"),
            "ticks": st["ticks"],
            "buy_vol": round(st["buy"], 2),
            "sell_vol": round(st["sell"], 2),
            "unk_vol": round(st["unk"], 2),
            "delta_ratio": round((st["buy"] - st["sell"]) / total, 4) if total > 0 else None,
            "l1_imb": book.get("l1_imb"),
            "l5_imb": book.get("l5_imb"),
            "l5_imb_avg": round(st["imb_sum"] / st["imb_n"], 4) if st["imb_n"] else None,
            "tot_imb": book.get("tot_imb"),
            "spread_bps": book.get("spread_bps"),
            "micro_bps": book.get("micro_bps"),
        }

    def _write(self, row):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, default=str, separators=(",", ":")) + "\n")
            self.rows_written += 1
        except Exception as exc:  # recorder must never break the stream
            self.last_error = str(exc)

    def status(self):
        return {
            "schema": SCHEMA,
            "rows_written": self.rows_written,
            "symbols_live": len(self._state),
            "last_error": self.last_error,
            "file": str(self.path) if self.path else None,
        }
