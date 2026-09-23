"""V12.3 full-universe underlying observer.

Why this exists
---------------
Previous live layers first shortlisted a handful of stocks and only then
started fast observation. That can miss price-led moves whose OI/option
confirmation arrives later. V12.3 reverses the order:

    all F&O cash underlyings -> lightweight live observation -> event family
    -> persistent Focus Desk -> deep futures/options routing.

This stream deliberately does NOT score a stock 0-100 and does not place
orders.  It detects concrete price/participation events.  It uses a third
KiteTicker connection in QUOTE mode; the deep V12.2B stream remains bounded
and uses FULL mode only for Focus-Desk symbols.

The service is fail-soft. If the extra WebSocket cannot connect, the normal
15-minute scanner, Trial-25, V12 and V12.1 recorders continue unaffected.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import threading
import time
from collections import defaultdict, deque
from typing import Callable

from . import scanner, v123_state


SAMPLE_SECONDS = 5
MAX_SAMPLE_MINUTES = 45
PUBLISH_SECONDS = 2
MAX_DISCOVERY_EVENTS = 30
MAX_MOVER_ROWS = 16


def _f(value, default=None):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _dt(value):
    if isinstance(value, dt.datetime):
        return value
    if value is None:
        return None
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _iso(value):
    return value.isoformat(timespec="seconds") if isinstance(value, dt.datetime) else None


def _market_open(now):
    if now.weekday() >= 5:
        return False
    minute = now.hour * 60 + now.minute
    return 9 * 60 + 15 <= minute <= 15 * 60 + 30


def _ticker_factory(api_key, access_token):
    from kiteconnect import KiteTicker
    return KiteTicker(api_key, access_token)


def _reactor_getter():
    try:
        from twisted.internet import reactor
        return reactor
    except Exception:
        return None


def _pct(a, b):
    if a is None or b is None or b == 0:
        return None
    return (a / b - 1.0) * 100.0


def _signed_ok(direction, value, minimum=0.0):
    if value is None:
        return False
    return value >= minimum if direction == "Bullish" else value <= -minimum


class UniverseMomentumStreamService:
    """Lightweight full-F&O underlying observer.

    QUOTE mode is enough here: price, day OHLC and cumulative volume.  Futures
    depth/options are intentionally deferred until a symbol is promoted into
    the Focus Desk.
    """

    def __init__(
        self,
        *,
        publish_callback: Callable[[dict], None],
        metadata_provider: Callable[[], list[dict]],
        access_token_getter,
        kite_client_getter,
        api_key: str,
        ticker_factory=None,
        now_provider=None,
        sleep_fn=time.sleep,
        reactor_getter=None,
        checkpoint_path=None,
    ):
        self.publish_callback = publish_callback
        self.metadata_provider = metadata_provider
        self.access_token_getter = access_token_getter
        self.kite_client_getter = kite_client_getter
        self.api_key = api_key
        self.ticker_factory = ticker_factory or _ticker_factory
        self.now_provider = now_provider or scanner.now_ist
        self.sleep_fn = sleep_fn
        self.reactor_getter = reactor_getter or _reactor_getter
        self.checkpoint_path = checkpoint_path

        self._lock = threading.RLock()
        self._ticker = None
        self._active = False
        self._connected = False
        self._tokens = {}
        self._token_to_symbol = {}
        self._samples = defaultdict(lambda: deque(maxlen=max(120, int(MAX_SAMPLE_MINUTES * 60 / SAMPLE_SECONDS) + 20)))
        self._latest = {}
        self._last_sample_at = {}
        self._last_publish_at = None
        self._last_checkpoint_at = None
        self._checkpoint_restored = False
        self._last_error = None
        self._snapshot = {
            "status": "WAITING",
            "universe_count": 0,
            "events": [],
            "leaders": [],
            "laggards": [],
            "validation_label": "UNDERLYING-FIRST / DECISION SUPPORT",
        }

    def snapshot(self):
        with self._lock:
            return json.loads(json.dumps(self._snapshot, default=str))

    def _restore_checkpoint(self, now):
        if self._checkpoint_restored:
            return
        self._checkpoint_restored = True
        if not self.checkpoint_path:
            return
        payload = v123_state.load_observer_checkpoint(self.checkpoint_path, now=now)
        if not payload:
            return

        restored = 0
        with self._lock:
            for symbol, rows in (payload.get("symbols") or {}).items():
                bucket = self._samples[symbol]
                for row in rows or []:
                    ts = _dt(row.get("ts"))
                    if ts is None:
                        continue
                    bucket.append({
                        "ts": ts,
                        "price": _f(row.get("price")),
                        "volume": _f(row.get("volume")),
                        "open": None,
                        "high": _f(row.get("high")),
                        "low": _f(row.get("low")),
                        "prev_close": _f(row.get("prev_close")),
                    })
                    restored += 1
                if bucket:
                    self._last_sample_at[symbol] = bucket[-1]["ts"]

            for symbol, tick in (payload.get("latest") or {}).items():
                if isinstance(tick, dict):
                    self._latest[symbol] = dict(tick)

        if restored:
            self._snapshot["checkpoint_restored_samples"] = restored
            self._snapshot["checkpoint_saved_at"] = payload.get("saved_at")

    def _maybe_checkpoint(self, now, force=False):
        if not self.checkpoint_path:
            return
        if not force and self._last_checkpoint_at is not None:
            if (now - self._last_checkpoint_at).total_seconds() < v123_state.OBSERVER_CHECKPOINT_SECONDS:
                return
        with self._lock:
            samples = {symbol: list(rows) for symbol, rows in self._samples.items() if rows}
            latest = {symbol: dict(tick) for symbol, tick in self._latest.items()}
        try:
            v123_state.save_observer_checkpoint(
                self.checkpoint_path, samples, latest, now=now
            )
            self._last_checkpoint_at = now
        except Exception as exc:
            # Checkpointing is operational safety; it must never stop ticks.
            with self._lock:
                self._last_error = "observer checkpoint failed: %s" % exc

    def _dispatch(self, fn):
        reactor = self.reactor_getter()
        if reactor is not None and getattr(reactor, "running", False):
            reactor.callFromThread(fn)
        else:
            fn()

    def _publish(self, payload):
        payload = dict(payload or {})
        payload["validation_label"] = "UNDERLYING-FIRST / DECISION SUPPORT"
        payload["updated_at"] = _iso(self.now_provider())
        with self._lock:
            self._snapshot = payload
        try:
            self.publish_callback(payload)
        except Exception:
            pass

    def _resolve_universe(self, kite):
        symbols = scanner.get_fno_stock_list(kite)
        token_map = scanner.cached_nse_instrument_tokens(symbols)
        # get_fno_stock_list() has already loaded the cash map.  Skip any
        # derivative name that still lacks a cash token rather than guessing.
        tokens = {str(sym): int(tok) for sym, tok in token_map.items() if tok}
        try:
            nifty = scanner.get_index_token(kite, "NIFTY 50")
        except Exception:
            nifty = None
        if nifty:
            tokens["NIFTY 50"] = int(nifty)
        return tokens

    def _metadata(self):
        rows = list(self.metadata_provider() or [])
        return {str(r.get("symbol")): dict(r) for r in rows if r.get("symbol") and not r.get("error")}

    def _append_sample(self, symbol, tick, now):
        price = _f(tick.get("last_price"))
        if price is None or price <= 0:
            return
        last = self._last_sample_at.get(symbol)
        if last is not None and (now - last).total_seconds() < SAMPLE_SECONDS:
            return

        volume = _f(tick.get("volume_traded"), _f(tick.get("volume")))
        ohlc = tick.get("ohlc") or {}
        sample = {
            "ts": now,
            "price": price,
            "volume": volume,
            "open": _f(ohlc.get("open")),
            "high": _f(ohlc.get("high")),
            "low": _f(ohlc.get("low")),
            "prev_close": _f(ohlc.get("close")),
        }
        self._samples[symbol].append(sample)
        self._last_sample_at[symbol] = now

    def _sample_at(self, symbol, now, seconds):
        target = now - dt.timedelta(seconds=seconds)
        rows = self._samples.get(symbol) or ()
        best = None
        for sample in reversed(rows):
            if sample["ts"] <= target:
                best = sample
                break
        return best

    def _return(self, symbol, now, seconds):
        rows = self._samples.get(symbol) or ()
        if not rows:
            return None
        current = rows[-1]
        past = self._sample_at(symbol, now, seconds)
        if past is None:
            return None
        return _pct(_f(current.get("price")), _f(past.get("price")))

    def _volume_accel(self, symbol, now):
        """Recent 60s volume rate divided by the preceding 60s rate.

        This is an event/transition measure, not absolute RVOL. Cumulative
        volume resets only once per session, so differencing it is robust to
        symbol-specific activity levels.
        """
        rows = self._samples.get(symbol) or ()
        if len(rows) < 4:
            return None
        cur = rows[-1]
        m1 = self._sample_at(symbol, now, 60)
        m2 = self._sample_at(symbol, now, 120)
        if m1 is None or m2 is None:
            return None
        cv, v1, v2 = _f(cur.get("volume")), _f(m1.get("volume")), _f(m2.get("volume"))
        if None in (cv, v1, v2):
            return None
        recent = max(0.0, cv - v1)
        prior = max(0.0, v1 - v2)
        if prior <= 0:
            return None
        return round(recent / prior, 3)

    def _near_extreme(self, sample, direction):
        price = _f(sample.get("price"))
        high = _f(sample.get("high"))
        low = _f(sample.get("low"))
        if price is None:
            return False
        if direction == "Bullish" and high and high > 0:
            return (high - price) / high <= 0.0015
        if direction == "Bearish" and low and low > 0:
            return (price - low) / low <= 0.0015
        return False

    def _continuation_reclaim(self, symbol, direction, now, day_change):
        if day_change is None or abs(day_change) < 1.0:
            return False
        cur_rows = self._samples.get(symbol) or ()
        if not cur_rows:
            return False
        current = _f(cur_rows[-1].get("price"))
        p3 = self._sample_at(symbol, now, 180)
        p8 = self._sample_at(symbol, now, 480)
        if current is None or p3 is None or p8 is None:
            return False
        x3, x8 = _f(p3.get("price")), _f(p8.get("price"))
        if x3 is None or x8 is None:
            return False
        if direction == "Bullish":
            had_pullback = x3 < x8
            reclaim = _pct(current, x3)
            return had_pullback and reclaim is not None and reclaim >= 0.12
        had_pullback = x3 > x8
        reclaim = _pct(current, x3)
        return had_pullback and reclaim is not None and reclaim <= -0.12

    def _event_for(self, symbol, sample, meta, nifty_returns, now):
        price = _f(sample.get("price"))
        prev = _f(sample.get("prev_close"), _f(meta.get("prev_close")))
        if price is None or prev is None or prev <= 0:
            return None

        day = _pct(price, prev)
        r1 = self._return(symbol, now, 60)
        r3 = self._return(symbol, now, 180)
        r5 = self._return(symbol, now, 300)
        r10 = self._return(symbol, now, 600)
        vol_accel = self._volume_accel(symbol, now)
        n5 = nifty_returns.get("5m")
        rel5 = None if r5 is None or n5 is None else round(r5 - n5, 4)

        # Direction is derived from the live underlying event itself, not from
        # legacy indicator votes.
        live_axis = r3 if r3 is not None else (r5 if r5 is not None else day)
        if live_axis is None or abs(live_axis) < 0.05:
            direction = "Bullish" if day >= 0 else "Bearish"
        else:
            direction = "Bullish" if live_axis > 0 else "Bearish"

        at_extreme = self._near_extreme(sample, direction)
        minute = now.hour * 60 + now.minute
        opening = 9 * 60 + 15 <= minute <= 10 * 60 + 15

        event_family = None
        why = []

        # Opening-drive: price is already pressing the session extreme while
        # both short return and participation are expanding.
        if (
            opening and _signed_ok(direction, r5, 0.30)
            and vol_accel is not None and vol_accel >= 1.20
            and at_extreme
            and (rel5 is None or _signed_ok(direction, rel5, 0.08))
        ):
            event_family = "OPENING_DRIVE"
            why = ["5m directional expansion", "volume rate accelerating", "pressing session extreme"]

        # Range expansion is available all day and does not need old OI.
        elif (
            _signed_ok(direction, r5, 0.40)
            and vol_accel is not None and vol_accel >= 1.20
            and at_extreme
            and (rel5 is None or _signed_ok(direction, rel5, 0.08))
        ):
            event_family = "RANGE_EXPANSION"
            why = ["5m range expansion", "volume rate accelerating", "session extreme"]

        # Relative leader/laggard catches stock-specific movement even when
        # absolute market activity is modest.
        elif (
            rel5 is not None and _signed_ok(direction, rel5, 0.30)
            and day is not None and _signed_ok(direction, day, 0.75)
            and r3 is not None and _signed_ok(direction, r3, 0.10)
        ):
            event_family = "RELATIVE_SEPARATION"
            why = ["stock separating from NIFTY", "same-direction 3m continuation"]

        # Continuation/re-entry is explicitly separate from the first early
        # entry.  A stock is not deleted merely because the initial move is
        # already underway.
        elif self._continuation_reclaim(symbol, direction, now, day):
            event_family = "PULLBACK_RECLAIM"
            why = ["existing day trend", "short pullback", "live reclaim"]

        # Sustained trend catches strong movers whose first expansion was
        # missed but which are still moving in an orderly fashion.
        elif (
            day is not None and _signed_ok(direction, day, 1.0)
            and r3 is not None and r10 is not None
            and _signed_ok(direction, r3, 0.12)
            and _signed_ok(direction, r10, 0.35)
        ):
            event_family = "MOMENTUM_CONTINUATION"
            why = ["strong day move", "3m and 10m direction agree"]

        if event_family is None:
            return None

        atr = _f(meta.get("atr"))
        move_atr = None
        if atr and atr > 0 and prev:
            move_atr = abs(price - prev) / atr

        return {
            "symbol": symbol,
            "direction": direction,
            "event_family": event_family,
            "detected_at": _iso(now),
            "live_price": round(price, 4),
            "day_change_pct": round(day, 4) if day is not None else None,
            "ret_1m_pct": round(r1, 4) if r1 is not None else None,
            "ret_3m_pct": round(r3, 4) if r3 is not None else None,
            "ret_5m_pct": round(r5, 4) if r5 is not None else None,
            "ret_10m_pct": round(r10, 4) if r10 is not None else None,
            "relative_5m_vs_nifty_pct": rel5,
            "volume_rate_accel": vol_accel,
            "near_session_extreme": at_extreme,
            "move_from_prev_close_atr": round(move_atr, 3) if move_atr is not None else None,
            "atr": atr,
            "prev_close": prev,
            "sector": meta.get("sector"),
            "oi_chg_15m_pct": meta.get("oi_chg_15m_pct"),
            "oi_acceleration": meta.get("oi_acceleration"),
            "tod_rvol": meta.get("tod_rvol"),
            "why": why,
        }

    def _build_snapshot(self, now):
        meta = self._metadata()
        with self._lock:
            latest = {k: dict(v) for k, v in self._latest.items()}
            connected = self._connected
            last_error = self._last_error

        nifty_returns = {
            "3m": self._return("NIFTY 50", now, 180),
            "5m": self._return("NIFTY 50", now, 300),
            "10m": self._return("NIFTY 50", now, 600),
        }

        events = []
        movers = []
        for symbol, tick in latest.items():
            if symbol == "NIFTY 50":
                continue
            sample_rows = self._samples.get(symbol) or ()
            if not sample_rows:
                continue
            sample = sample_rows[-1]
            prev = _f(sample.get("prev_close"), _f((meta.get(symbol) or {}).get("prev_close")))
            price = _f(sample.get("price"))
            day = _pct(price, prev)
            r5 = self._return(symbol, now, 300)
            if day is not None:
                movers.append({
                    "symbol": symbol,
                    "live_price": round(price, 4) if price is not None else None,
                    "day_change_pct": round(day, 4),
                    "ret_5m_pct": round(r5, 4) if r5 is not None else None,
                })
            event = self._event_for(symbol, sample, meta.get(symbol) or {}, nifty_returns, now)
            if event:
                events.append(event)

        priority = {
            "PULLBACK_RECLAIM": 6,
            "OPENING_DRIVE": 5,
            "RANGE_EXPANSION": 4,
            "RELATIVE_SEPARATION": 3,
            "MOMENTUM_CONTINUATION": 2,
        }
        events.sort(
            key=lambda x: (
                priority.get(x.get("event_family"), 0),
                abs(_f(x.get("relative_5m_vs_nifty_pct"), 0.0)),
                abs(_f(x.get("ret_5m_pct"), 0.0)),
            ),
            reverse=True,
        )

        leaders = sorted(movers, key=lambda x: _f(x.get("day_change_pct"), -999), reverse=True)[:MAX_MOVER_ROWS]
        laggards = sorted(movers, key=lambda x: _f(x.get("day_change_pct"), 999))[:MAX_MOVER_ROWS]

        return {
            "status": "STREAMING" if connected else "CONNECTING",
            "universe_count": max(0, len(self._tokens) - (1 if "NIFTY 50" in self._tokens else 0)),
            "nifty": {
                "ret_3m_pct": nifty_returns["3m"],
                "ret_5m_pct": nifty_returns["5m"],
                "ret_10m_pct": nifty_returns["10m"],
            },
            "events": events[:MAX_DISCOVERY_EVENTS],
            "leaders": leaders,
            "laggards": laggards,
            "last_error": last_error,
        }

    def _maybe_publish(self, now, force=False):
        if not force and self._last_publish_at is not None:
            if (now - self._last_publish_at).total_seconds() < PUBLISH_SECONDS:
                self._maybe_checkpoint(now)
                return
        self._last_publish_at = now
        self._publish(self._build_snapshot(now))
        self._maybe_checkpoint(now, force=force)

    def _handle_ticks(self, ticks, now):
        with self._lock:
            token_to_symbol = dict(self._token_to_symbol)
        for raw in ticks or []:
            token = raw.get("instrument_token")
            if token is None:
                continue
            symbol = token_to_symbol.get(int(token))
            if not symbol:
                continue
            tick = dict(raw)
            tick["_received_at"] = _iso(now)
            with self._lock:
                self._latest[symbol] = tick
            self._append_sample(symbol, tick, now)
        self._maybe_publish(now)

    def _connect(self, access_token):
        ticker = self.ticker_factory(self.api_key, access_token)
        with self._lock:
            self._ticker = ticker
            self._active = True

        def on_connect(ws, response):
            with self._lock:
                tokens = sorted(self._token_to_symbol)
            if tokens:
                ws.subscribe(tokens)
                ws.set_mode(ws.MODE_QUOTE, tokens)
            with self._lock:
                self._connected = True
                self._last_error = None
            self._maybe_publish(self.now_provider(), force=True)

        def on_ticks(ws, ticks):
            with self._lock:
                if ws is not self._ticker:
                    return
            self._handle_ticks(ticks, self.now_provider())

        def on_close(ws, code, reason):
            with self._lock:
                if ws is self._ticker:
                    self._connected = False
                    self._last_error = str(reason or "websocket closed")
            self._maybe_publish(self.now_provider(), force=True)

        def on_error(ws, code, reason):
            with self._lock:
                self._last_error = str(reason or code)
            self._maybe_publish(self.now_provider(), force=True)

        def on_noreconnect(ws):
            with self._lock:
                if ws is self._ticker:
                    self._connected = False
                    self._active = False
                    self._ticker = None

        ticker.on_connect = on_connect
        ticker.on_ticks = on_ticks
        ticker.on_close = on_close
        ticker.on_error = on_error
        ticker.on_noreconnect = on_noreconnect

        def connect():
            try:
                ticker.connect(threaded=True)
            except Exception as exc:
                with self._lock:
                    self._active = False
                    self._connected = False
                    self._ticker = None
                    self._last_error = str(exc)
                self._maybe_publish(self.now_provider(), force=True)

        self._dispatch(connect)

    def _close(self):
        with self._lock:
            ticker = self._ticker
            self._ticker = None
            self._active = False
            self._connected = False
        if ticker is not None:
            self._dispatch(lambda: ticker.close())
        try:
            self._maybe_checkpoint(self.now_provider(), force=True)
        except Exception:
            pass

    def run_forever(self):
        while True:
            now = self.now_provider()
            self._restore_checkpoint(now)
            try:
                if not _market_open(now):
                    if self._active:
                        self._close()
                    self._publish({
                        "status": "MARKET_CLOSED",
                        "universe_count": max(0, len(self._tokens) - (1 if "NIFTY 50" in self._tokens else 0)),
                        "events": [], "leaders": [], "laggards": [],
                    })
                    self.sleep_fn(30)
                    continue

                kite = self.kite_client_getter()
                access_token = self.access_token_getter()
                if kite is None or not access_token:
                    self._publish({"status": "WAITING_LOGIN", "universe_count": 0, "events": [], "leaders": [], "laggards": []})
                    self.sleep_fn(10)
                    continue

                if not self._tokens:
                    tokens = self._resolve_universe(kite)
                    with self._lock:
                        self._tokens = dict(tokens)
                        self._token_to_symbol = {int(tok): sym for sym, tok in tokens.items()}

                if self._tokens and not self._active:
                    self._connect(access_token)
                else:
                    self._maybe_publish(now)
                self.sleep_fn(2)
            except Exception as exc:
                with self._lock:
                    self._last_error = str(exc)
                self._publish({
                    **self.snapshot(),
                    "status": "ERROR",
                    "last_error": str(exc),
                })
                self.sleep_fn(5)
