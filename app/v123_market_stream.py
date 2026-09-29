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
import logging
import math
import threading
import time
from collections import defaultdict, deque
from typing import Callable


from . import scanner, v123_state


log = logging.getLogger(__name__)

SAMPLE_SECONDS = 5
MAX_SAMPLE_MINUTES = 45
PUBLISH_SECONDS = 2
MAX_LOOKBACK_LAG_SECONDS = 45
MAX_DISCOVERY_EVENTS = 30
MAX_MOVER_ROWS = 30
LIVE_SAMPLE_STALE_SECONDS = 45
RECONNECT_COOLDOWN_SECONDS = 10

# Persistent Directional Travel (PDT) v2.
# Direction is a slow underlying regime; 3m remains execution-only.
DIRECTION_UPDATE_SECONDS = 55
DIRECTION_PATH_MINUTES = 15
DIRECTION_PATH_EFFICIENCY_MIN = 0.35
DIRECTION_MOVE_ATR_MIN = 0.20
DIRECTION_ENTER_VOTES = 3
DIRECTION_VOTE_WINDOW = 4
DIRECTION_EMA_FAST = 9
DIRECTION_EMA_SLOW = 20
DIRECTION_EMA_SLOPE_POINTS = 3


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
    second = now.hour * 3600 + now.minute * 60 + now.second
    return 9 * 3600 + 15 * 60 <= second < 15 * 3600 + 30 * 60


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
        # Context indices share the lightweight QUOTE socket but are never
        # eligible as stock movers.  This gives Spotting a contemporaneous
        # market + sector frame without adding a separate data connection.
        self._underlying_symbols = set()
        self._context_symbols = {"NIFTY 50"}
        self._samples = defaultdict(lambda: deque(maxlen=max(120, int(MAX_SAMPLE_MINUTES * 60 / SAMPLE_SECONDS) + 20)))
        self._latest = {}
        self._last_sample_at = {}
        self._direction_locks = {}
        self._last_publish_at = None
        self._last_checkpoint_at = None
        self._checkpoint_restored = False
        self._last_error = None
        self._last_tick_at = None
        self._last_connect_at = None
        self._last_disconnect_at = None
        self._connection_count = 0
        self._reconnect_count = 0
        self._next_connect_at = None
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

            for symbol, state in (payload.get("direction_locks") or {}).items():
                if isinstance(state, dict):
                    self._direction_locks[str(symbol)] = dict(state)

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
            locks = {symbol: dict(state) for symbol, state in self._direction_locks.items()}
            v123_state.save_observer_checkpoint(
                self.checkpoint_path, samples, latest, direction_locks=locks, now=now
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
        underlying_symbols = set(tokens)
        context_symbols = {"NIFTY 50"}

        try:
            nifty = scanner.get_index_token(kite, "NIFTY 50")
        except Exception:
            nifty = None
        if nifty:
            tokens["NIFTY 50"] = int(nifty)

        # Reuse the sector map already maintained by the scanner. Sector
        # indices are context-only: subscribed in QUOTE mode, excluded from
        # discovery/mover rows, and consumed by the quant shadow as factors.
        for sector in getattr(scanner, "SECTOR_INDEXES", ()):
            try:
                token = scanner.get_index_token(kite, sector)
            except Exception:
                token = None
            if token:
                tokens[str(sector)] = int(token)
                context_symbols.add(str(sector))

        with self._lock:
            self._underlying_symbols = underlying_symbols
            self._context_symbols = context_symbols
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

    def _sample_at(self, symbol, now, seconds, *, max_lag_seconds=MAX_LOOKBACK_LAG_SECONDS):
        target = now - dt.timedelta(seconds=seconds)
        rows = self._samples.get(symbol) or ()
        best = None
        for sample in reversed(rows):
            if sample["ts"] <= target:
                best = sample
                break
        if best is None:
            return None
        # Never reuse an arbitrarily old sample for several lookback windows
        # after a restart/data gap.  If the requested timestamp is not
        # represented closely enough, the feature is unavailable.
        lag = (target - best["ts"]).total_seconds()
        if lag < 0 or lag > float(max_lag_seconds):
            return None
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


    @staticmethod
    def _ema_values(values, span):
        alpha = 2.0 / (float(span) + 1.0)
        out = []
        level = None
        for value in values:
            value = float(value)
            level = value if level is None else alpha * value + (1.0 - alpha) * level
            out.append(level)
        return out

    def _minute_prices(self, symbol, now, minutes):
        rows = self._samples.get(symbol) or ()
        if not rows:
            return []
        points = []
        current = rows[-1]
        cts = _dt(current.get("ts"))
        cp = _f(current.get("price"))
        if cts is not None and cp is not None:
            points.append((cts, cp))
        for minute in range(1, int(minutes) + 1):
            sample = self._sample_at(symbol, now, minute * 60)
            if sample is None:
                continue
            ts = _dt(sample.get("ts"))
            px = _f(sample.get("price"))
            if ts is None or px is None:
                continue
            points.append((ts, px))
        # One point per represented minute, chronological. Samples can repeat
        # after a small feed gap, so timestamp dedupe is required.
        dedup = {}
        for ts, px in points:
            dedup[ts] = px
        return sorted(dedup.items())

    def _direction_regime_features(
        self, symbol, now, *, atr=None, market_15m_pct=None, sector_15m_pct=None
    ):
        points = self._minute_prices(symbol, now, DIRECTION_EMA_SLOW + 4)
        if len(points) < 12:
            return {
                "candidate": None,
                "path_efficiency": None,
                "move_15m_atr": None,
                "ret_15m_pct": None,
                "ema9": None,
                "ema20": None,
                "ema9_slope": None,
                "relative_residual_15m_pct": None,
                "reason": "REGIME_HISTORY_NOT_READY",
            }

        # Keep the most recent ~15 one-minute intervals for efficiency.
        recent = points[-(DIRECTION_PATH_MINUTES + 1):]
        if len(recent) < 12:
            return {"candidate": None, "reason": "REGIME_HISTORY_NOT_READY"}

        prices = [px for _, px in recent]
        first = prices[0]
        last = prices[-1]
        path = sum(abs(prices[i] - prices[i - 1]) for i in range(1, len(prices)))
        signed_eff = (last - first) / path if path > 0 else 0.0
        ret15 = (last / first - 1.0) * 100.0 if first > 0 else None

        all_prices = [px for _, px in points]
        ema9s = self._ema_values(all_prices, DIRECTION_EMA_FAST)
        ema20s = self._ema_values(all_prices, DIRECTION_EMA_SLOW)
        ema9 = ema9s[-1]
        ema20 = ema20s[-1]
        slope_idx = max(0, len(ema9s) - 1 - DIRECTION_EMA_SLOPE_POINTS)
        ema9_slope = ema9s[-1] - ema9s[slope_idx]

        atr = _f(atr)
        move_atr = abs(last - first) / atr if atr and atr > 0 else None
        move_ok = (
            move_atr is not None and move_atr >= DIRECTION_MOVE_ATR_MIN
        ) or (
            move_atr is None and ret15 is not None and abs(ret15) >= 0.20
        )

        candidate = None
        bull = (
            signed_eff >= DIRECTION_PATH_EFFICIENCY_MIN
            and move_ok
            and ema9 > ema20
            and ema9_slope > 0
            and last >= ema20
        )
        bear = (
            signed_eff <= -DIRECTION_PATH_EFFICIENCY_MIN
            and move_ok
            and ema9 < ema20
            and ema9_slope < 0
            and last <= ema20
        )
        if bull:
            candidate = "Bullish"
        elif bear:
            candidate = "Bearish"

        residuals = []
        if ret15 is not None and market_15m_pct is not None:
            residuals.append(ret15 - float(market_15m_pct))
        if ret15 is not None and sector_15m_pct is not None:
            residuals.append(ret15 - float(sector_15m_pct))
        residual = sum(residuals) / len(residuals) if residuals else None

        return {
            "candidate": candidate,
            "path_efficiency": round(signed_eff, 4),
            "move_15m_atr": round(move_atr, 4) if move_atr is not None else None,
            "ret_15m_pct": round(ret15, 4) if ret15 is not None else None,
            "ema9": round(ema9, 4),
            "ema20": round(ema20, 4),
            "ema9_slope": round(ema9_slope, 6),
            "relative_residual_15m_pct": round(residual, 4) if residual is not None else None,
            "reason": "PERSISTENT_DIRECTIONAL_TRAVEL" if candidate else "REGIME_STRUCTURE_NOT_ALIGNED",
        }

    @staticmethod
    def _new_direction_lock():
        return {
            "state": "NEUTRAL",
            "phase": "NEUTRAL",
            "since": None,
            "last_eval_at": None,
            "observations": 0,
            "recent_candidates": [],
            "pending_direction": None,
            "path_efficiency": None,
            "move_15m_atr": None,
            "ret_15m_pct": None,
            "ema9": None,
            "ema20": None,
            "ema9_slope": None,
            "relative_residual_15m_pct": None,
        }

    def _update_direction_lock(
        self, symbol, now, *, atr=None, market_15m_pct=None, sector_15m_pct=None
    ):
        """Persistent Directional Travel lock.

        Direction is inferred from 15m path efficiency + ATR-normalised travel
        + EMA9/20 structure sampled on one-minute points.  The lock needs 3 of
        the last 4 regime observations.  Opposite direction first moves the
        state through NEUTRAL/REVERSAL_PENDING, so a short pullback cannot
        directly flip the desk.
        """
        state = self._direction_locks.setdefault(symbol, self._new_direction_lock())
        last_eval = _dt(state.get("last_eval_at"))
        if last_eval is not None and (now - last_eval).total_seconds() < DIRECTION_UPDATE_SECONDS:
            return self._direction_lock_summary(state)

        feat = self._direction_regime_features(
            symbol, now, atr=atr,
            market_15m_pct=market_15m_pct,
            sector_15m_pct=sector_15m_pct,
        )
        candidate = feat.get("candidate")
        vote = 1 if candidate == "Bullish" else (-1 if candidate == "Bearish" else 0)
        votes = list(state.get("recent_candidates") or [])
        votes.append(vote)
        votes = votes[-DIRECTION_VOTE_WINDOW:]
        state["recent_candidates"] = votes
        state["observations"] = int(state.get("observations") or 0) + 1
        state["last_eval_at"] = _iso(now)
        for key in (
            "path_efficiency", "move_15m_atr", "ret_15m_pct",
            "ema9", "ema20", "ema9_slope", "relative_residual_15m_pct",
        ):
            state[key] = feat.get(key)

        bull_votes = sum(v > 0 for v in votes)
        bear_votes = sum(v < 0 for v in votes)
        prior = str(state.get("state") or "NEUTRAL")
        pending = state.get("pending_direction")

        if prior == "BULLISH":
            if bear_votes >= DIRECTION_ENTER_VOTES:
                state["state"] = "NEUTRAL"
                state["phase"] = "REVERSAL_PENDING"
                state["pending_direction"] = "BEARISH"
                state["since"] = None
            elif candidate == "Bullish":
                state["phase"] = "CONTINUING"
            elif candidate is None:
                state["phase"] = "PULLBACK" if (_f(self._return(symbol, now, 60), 0.0) or 0.0) < 0 else "WEAKENING"
        elif prior == "BEARISH":
            if bull_votes >= DIRECTION_ENTER_VOTES:
                state["state"] = "NEUTRAL"
                state["phase"] = "REVERSAL_PENDING"
                state["pending_direction"] = "BULLISH"
                state["since"] = None
            elif candidate == "Bearish":
                state["phase"] = "CONTINUING"
            elif candidate is None:
                state["phase"] = "PULLBACK" if (_f(self._return(symbol, now, 60), 0.0) or 0.0) > 0 else "WEAKENING"
        else:
            if pending == "BULLISH":
                if bull_votes >= DIRECTION_ENTER_VOTES:
                    state["state"] = "BULLISH"
                    state["phase"] = "CONTINUING"
                    state["since"] = _iso(now)
                    state["pending_direction"] = None
            elif pending == "BEARISH":
                if bear_votes >= DIRECTION_ENTER_VOTES:
                    state["state"] = "BEARISH"
                    state["phase"] = "CONTINUING"
                    state["since"] = _iso(now)
                    state["pending_direction"] = None
            elif bull_votes >= DIRECTION_ENTER_VOTES:
                state["state"] = "BULLISH"
                state["phase"] = "CONTINUING"
                state["since"] = _iso(now)
            elif bear_votes >= DIRECTION_ENTER_VOTES:
                state["state"] = "BEARISH"
                state["phase"] = "CONTINUING"
                state["since"] = _iso(now)
            else:
                state["phase"] = "NEUTRAL"

        return self._direction_lock_summary(state)

    @staticmethod
    def _direction_lock_summary(state):
        locked = str(state.get("state") or "NEUTRAL")
        votes = list(state.get("recent_candidates") or [])
        return {
            "state": locked,
            "direction": "Bullish" if locked == "BULLISH" else (
                "Bearish" if locked == "BEARISH" else None
            ),
            "phase": state.get("phase") or "NEUTRAL",
            "since": state.get("since"),
            "observations": int(state.get("observations") or 0),
            "up_evidence": sum(v > 0 for v in votes),
            "down_evidence": sum(v < 0 for v in votes),
            "pending_direction": state.get("pending_direction"),
            "path_efficiency": state.get("path_efficiency"),
            "move_15m_atr": state.get("move_15m_atr"),
            "ret_15m_pct": state.get("ret_15m_pct"),
            "ema9": state.get("ema9"),
            "ema20": state.get("ema20"),
            "ema9_slope": state.get("ema9_slope"),
            "relative_residual_15m_pct": state.get("relative_residual_15m_pct"),
        }

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

    def _event_for(self, symbol, sample, meta, nifty_returns, now, direction_info=None):
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

        direction = str((direction_info or {}).get("direction") or "")
        if direction not in ("Bullish", "Bearish"):
            return None

        at_extreme = self._near_extreme(sample, direction)
        minute = now.hour * 60 + now.minute
        opening = 9 * 60 + 15 <= minute <= 10 * 60 + 15

        event_family = None
        why = []

        # Primary production lane: a persistent underlying regime can enter
        # Focus without waiting for a legacy event label.  Legacy families
        # remain useful for entry/re-entry context, but they no longer decide
        # whether an obvious trend exists.
        if (
            str((direction_info or {}).get("phase") or "") == "CONTINUING"
            and abs(_f((direction_info or {}).get("path_efficiency"), 0.0) or 0.0) >= DIRECTION_PATH_EFFICIENCY_MIN
            and (
                _f((direction_info or {}).get("move_15m_atr")) is None
                or _f((direction_info or {}).get("move_15m_atr"), 0.0) >= DIRECTION_MOVE_ATR_MIN
            )
        ):
            event_family = "REGIME_PERSISTENCE"
            why = ["15m directional path efficient", "EMA9/20 structure aligned", "direction lock persistent"]

        # Opening-drive: price is already pressing the session extreme while
        # both short return and participation are expanding.
        elif (
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
            "direction_lock_state": (direction_info or {}).get("state"),
            "direction_lock_phase": (direction_info or {}).get("phase"),
            "direction_lock_since": (direction_info or {}).get("since"),
            "direction_up_evidence": (direction_info or {}).get("up_evidence"),
            "direction_down_evidence": (direction_info or {}).get("down_evidence"),
            "direction_residual_z": (direction_info or {}).get("relative_residual_15m_pct"),
            "path_efficiency_15m": (direction_info or {}).get("path_efficiency"),
            "move_15m_atr": (direction_info or {}).get("move_15m_atr"),
            "ret_15m_pct": (direction_info or {}).get("ret_15m_pct"),
            "ema9_live": (direction_info or {}).get("ema9"),
            "ema20_live": (direction_info or {}).get("ema20"),
            "ema9_slope_live": (direction_info or {}).get("ema9_slope"),
            "why": why,
        }

    def _event_diagnostic(self, symbol, sample, meta, nifty_returns, now, event=None, direction_info=None):
        """Explain why an actual mover did or did not qualify for discovery."""
        price = _f(sample.get("price"))
        prev = _f(sample.get("prev_close"), _f(meta.get("prev_close")))
        if price is None or prev is None or prev <= 0:
            return {"qualified": False, "reason": "MISSING_PRICE_OR_PREV_CLOSE", "failed_gates": ["price/prev_close"]}

        day = _pct(price, prev)
        r3 = self._return(symbol, now, 180)
        r5 = self._return(symbol, now, 300)
        r10 = self._return(symbol, now, 600)
        vol_accel = self._volume_accel(symbol, now)
        n5 = nifty_returns.get("5m")
        rel5 = None if r5 is None or n5 is None else round(r5 - n5, 4)
        direction = str((direction_info or {}).get("direction") or "")
        if direction not in ("Bullish", "Bearish"):
            return {
                "qualified": False,
                "reason": "DIRECTION_NOT_LOCKED",
                "failed_gates": ["sequential direction evidence below lock boundary"],
                "direction": None,
                "direction_lock_state": (direction_info or {}).get("state", "NEUTRAL"),
                "direction_lock_phase": (direction_info or {}).get("phase", "NEUTRAL"),
                "ret_3m_pct": r3, "ret_5m_pct": r5, "ret_10m_pct": r10,
                "relative_5m_vs_nifty_pct": rel5, "volume_rate_accel": vol_accel,
                "near_session_extreme": False,
            }
        at_extreme = self._near_extreme(sample, direction)
        sign = 1.0 if direction == "Bullish" else -1.0

        if event:
            return {
                "qualified": True,
                "reason": str(event.get("event_family") or "QUALIFIED_EVENT"),
                "failed_gates": [],
                "direction": direction,
                "ret_3m_pct": r3, "ret_5m_pct": r5, "ret_10m_pct": r10,
                "relative_5m_vs_nifty_pct": rel5, "volume_rate_accel": vol_accel,
                "near_session_extreme": at_extreme,
            }

        failed = []
        if r5 is None:
            failed.append("5m history not ready")
        elif sign * r5 < 0.20:
            failed.append("5m move < 0.20%")
        if vol_accel is None:
            failed.append("volume acceleration unavailable")
        elif vol_accel < 1.20:
            failed.append("volume rate < 1.20x")
        if not at_extreme:
            failed.append("not near session extreme")
        if rel5 is not None and sign * rel5 < 0.08:
            failed.append("relative 5m < 0.08%")
        if day is not None and sign * day < 0.75:
            failed.append(f"direction-aligned day move {sign * day:.2f}% < 0.75%")
        if r3 is None or sign * r3 < 0.10:
            failed.append("3m continuation < 0.10%")
        if r10 is None or sign * r10 < 0.35:
            failed.append("10m continuation < 0.35%")
        if not self._continuation_reclaim(symbol, direction, now, day):
            failed.append("no fresh pullback-reclaim")

        # Report the few most informative unmet gates rather than dumping every
        # branch condition.
        return {
            "qualified": False,
            "reason": "NO_EVENT_FAMILY_QUALIFIED",
            "failed_gates": failed[:5],
            "direction": direction,
            "ret_3m_pct": r3, "ret_5m_pct": r5, "ret_10m_pct": r10,
            "relative_5m_vs_nifty_pct": rel5, "volume_rate_accel": vol_accel,
            "near_session_extreme": at_extreme,
        }


    def _feed_health(self, now):
        with self._lock:
            connected = self._connected
            active = self._active
            last_tick = self._last_tick_at
            last_connect = self._last_connect_at
            last_disconnect = self._last_disconnect_at
            last_error = self._last_error
            connection_count = self._connection_count
            reconnect_count = self._reconnect_count
        tick_age = None
        if isinstance(last_tick, dt.datetime):
            tick_age = max(0.0, (now - last_tick).total_seconds())
        fresh = bool(connected and tick_age is not None and tick_age <= LIVE_SAMPLE_STALE_SECONDS)
        return {
            "stream": "V123_FULL_FNO",
            "active": bool(active),
            "connected": bool(connected),
            "fresh": fresh,
            "last_tick_at": _iso(last_tick),
            "last_tick_age_seconds": round(tick_age, 1) if tick_age is not None else None,
            "last_connect_at": _iso(last_connect),
            "last_disconnect_at": _iso(last_disconnect),
            "connection_count": int(connection_count),
            "reconnect_count": int(reconnect_count),
            "last_error": last_error,
        }

    def _build_snapshot(self, now):
        meta = self._metadata()
        with self._lock:
            latest = {k: dict(v) for k, v in self._latest.items()}
            connected = self._connected
            active = self._active
            last_error = self._last_error
        feed_health = self._feed_health(now)

        nifty_tick = latest.get("NIFTY 50") or {}
        nifty_live = _f(nifty_tick.get("last_price"))
        nifty_prev = _f((nifty_tick.get("ohlc") or {}).get("close"))
        nifty_returns = {
            "1m": self._return("NIFTY 50", now, 60),
            "3m": self._return("NIFTY 50", now, 180),
            "5m": self._return("NIFTY 50", now, 300),
            "10m": self._return("NIFTY 50", now, 600),
            "15m": self._return("NIFTY 50", now, 900),
            "day": _pct(nifty_live, nifty_prev),
        }
        with self._lock:
            context_symbols = set(self._context_symbols)
            underlying_count = len(self._underlying_symbols)

        sector_contexts = {}
        for sector in sorted(context_symbols - {"NIFTY 50"}):
            sector_tick = latest.get(sector) or {}
            sector_live = _f(sector_tick.get("last_price"))
            sector_prev = _f((sector_tick.get("ohlc") or {}).get("close"))
            sector_contexts[sector] = {
                "live_price": sector_live,
                "day_change_pct": _pct(sector_live, sector_prev),
                "ret_1m_pct": self._return(sector, now, 60),
                "ret_3m_pct": self._return(sector, now, 180),
                "ret_5m_pct": self._return(sector, now, 300),
                "ret_10m_pct": self._return(sector, now, 600),
                "ret_15m_pct": self._return(sector, now, 900),
            }

        events = []
        movers = []
        for symbol, tick in latest.items():
            if symbol in context_symbols:
                continue
            sample_rows = self._samples.get(symbol) or ()
            if not sample_rows:
                continue
            sample = sample_rows[-1]
            sample_ts = _dt(sample.get("ts"))
            if sample_ts is None or (now - sample_ts).total_seconds() > LIVE_SAMPLE_STALE_SECONDS:
                continue
            prev = _f(sample.get("prev_close"), _f((meta.get(symbol) or {}).get("prev_close")))
            price = _f(sample.get("price"))
            day = _pct(price, prev)
            r5 = self._return(symbol, now, 300)
            symbol_meta = meta.get(symbol) or {}
            sector = symbol_meta.get("sector") or scanner.SYMBOL_SECTOR_MAP.get(symbol)
            sector_ctx = sector_contexts.get(str(sector)) if sector else None
            sector_day = _f((sector_ctx or {}).get("day_change_pct"))
            sector_r1 = _f((sector_ctx or {}).get("ret_1m_pct"))
            sector_r5 = _f((sector_ctx or {}).get("ret_5m_pct"))
            sector_r10 = _f((sector_ctx or {}).get("ret_10m_pct"))
            sector_r15 = _f((sector_ctx or {}).get("ret_15m_pct"))
            rel_sector5 = (
                round(r5 - sector_r5, 4)
                if r5 is not None and sector_r5 is not None else None
            )
            direction_info = self._update_direction_lock(
                symbol,
                now,
                atr=symbol_meta.get("atr"),
                market_15m_pct=nifty_returns.get("15m"),
                sector_15m_pct=sector_r15,
            )
            event = self._event_for(
                symbol, sample, symbol_meta, nifty_returns, now, direction_info=direction_info
            )
            diagnostic = self._event_diagnostic(
                symbol, sample, symbol_meta, nifty_returns, now,
                event=event, direction_info=direction_info
            )
            if event is not None:
                event["sector_index"] = sector
                event["sector_day_change_pct"] = sector_day
                event["sector_ret_5m_pct"] = sector_r5
                event["sector_ret_10m_pct"] = sector_r10
                event["relative_5m_vs_sector_pct"] = rel_sector5
                event["market_day_change_pct"] = nifty_returns.get("day")
                event["market_ret_5m_pct"] = nifty_returns.get("5m")
                event["market_ret_10m_pct"] = nifty_returns.get("10m")
            if day is not None:
                movers.append({
                    "symbol": symbol,
                    "live_price": round(price, 4) if price is not None else None,
                    "day_change_pct": round(day, 4),
                    "ret_3m_pct": diagnostic.get("ret_3m_pct"),
                    "ret_5m_pct": round(r5, 4) if r5 is not None else None,
                    "ret_10m_pct": diagnostic.get("ret_10m_pct"),
                    "relative_5m_vs_nifty_pct": diagnostic.get("relative_5m_vs_nifty_pct"),
                    "market_day_change_pct": nifty_returns.get("day"),
                    "market_ret_5m_pct": nifty_returns.get("5m"),
                    "market_ret_10m_pct": nifty_returns.get("10m"),
                    "volume_rate_accel": diagnostic.get("volume_rate_accel"),
                    "sector": sector,
                    "sector_index": sector,
                    "sector_day_change_pct": sector_day,
                    "sector_ret_5m_pct": sector_r5,
                    "sector_ret_10m_pct": sector_r10,
                    "relative_5m_vs_sector_pct": rel_sector5,
                    "near_session_extreme": diagnostic.get("near_session_extreme"),
                    "discovery_qualified": diagnostic.get("qualified"),
                    "discovery_reason": diagnostic.get("reason"),
                    "discovery_failed_gates": diagnostic.get("failed_gates"),
                    "direction_lock_state": direction_info.get("state"),
                    "direction_lock_phase": direction_info.get("phase"),
                    "direction_lock_since": direction_info.get("since"),
                    "direction_up_evidence": direction_info.get("up_evidence"),
                    "direction_down_evidence": direction_info.get("down_evidence"),
                    "direction_residual_z": direction_info.get("relative_residual_15m_pct"),
                    "path_efficiency_15m": direction_info.get("path_efficiency"),
                    "move_15m_atr": direction_info.get("move_15m_atr"),
                    "ret_15m_pct": direction_info.get("ret_15m_pct"),
                    "ema9_live": direction_info.get("ema9"),
                    "ema20_live": direction_info.get("ema20"),
                    "ema9_slope_live": direction_info.get("ema9_slope"),
                })
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
            "status": "STREAMING" if feed_health.get("fresh") else ("CONNECTING" if active else "DISCONNECTED"),
            "universe_count": underlying_count if underlying_count else max(
                0, len(self._tokens) - len(context_symbols)
            ),
            "fresh_symbol_count": len(movers),
            "feed_health": feed_health,
            "nifty": {
                "live_price": nifty_live,
                "day_change_pct": nifty_returns["day"],
                "ret_1m_pct": nifty_returns["1m"],
                "ret_3m_pct": nifty_returns["3m"],
                "ret_5m_pct": nifty_returns["5m"],
                "ret_10m_pct": nifty_returns["10m"],
                "ret_15m_pct": nifty_returns["15m"],
            },
            "sector_contexts": sector_contexts,
            # Internal whole-universe feed for the research-only quant shadow.
            # background.py removes this before publishing/storing the normal
            # observer payload, so it cannot enlarge public dashboard/API data.
            "quant_rows": [
                # Private whole-universe rows.  The quant shadow still consumes
                # only its locked feature vector, while the extra diagnostics
                # let observability explain every meaningful mover rather than
                # only the top-30 leader/laggard slices.
                dict(row)
                for row in movers
            ],
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
                self._last_tick_at = now
            self._append_sample(symbol, tick, now)
        # Keep the shared Twisted reactor callback lightweight.  Snapshot
        # building, Focus updates and forensic I/O run from run_forever(),
        # not from on_ticks().


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
            now = self.now_provider()
            with self._lock:
                self._connected = True
                self._last_error = None
                self._last_connect_at = now
                self._connection_count += 1
                self._next_connect_at = None
            log.info("V123_WS connected tokens=%s", len(tokens))

        def on_ticks(ws, ticks):
            with self._lock:
                if ws is not self._ticker:
                    return
            self._handle_ticks(ticks, self.now_provider())

        def on_close(ws, code, reason):
            now = self.now_provider()
            with self._lock:
                if ws is self._ticker:
                    self._connected = False
                    self._last_disconnect_at = now
                    self._last_error = str(reason or "websocket closed")
            log.warning("V123_WS closed code=%s reason=%s", code, reason)

        def on_error(ws, code, reason):
            with self._lock:
                self._last_error = str(reason or code)
            log.warning("V123_WS error code=%s reason=%s", code, reason)

        def on_reconnect(ws, attempts):
            with self._lock:
                if ws is self._ticker:
                    self._reconnect_count += 1
                    self._connected = False
            log.warning("V123_WS reconnect attempt=%s", attempts)

        def on_noreconnect(ws):
            now = self.now_provider()
            with self._lock:
                if ws is self._ticker:
                    self._connected = False
                    self._active = False
                    self._ticker = None
                    self._last_disconnect_at = now
                    self._next_connect_at = now + dt.timedelta(seconds=RECONNECT_COOLDOWN_SECONDS)
            log.error("V123_WS reconnect attempts exhausted")

        ticker.on_connect = on_connect
        ticker.on_ticks = on_ticks
        ticker.on_close = on_close
        ticker.on_error = on_error
        ticker.on_reconnect = on_reconnect
        ticker.on_noreconnect = on_noreconnect

        def connect():
            try:
                ticker.connect(threaded=True)
            except Exception as exc:
                now = self.now_provider()
                with self._lock:
                    self._active = False
                    self._connected = False
                    self._ticker = None
                    self._last_error = str(exc)
                    self._last_disconnect_at = now
                    self._next_connect_at = now + dt.timedelta(seconds=RECONNECT_COOLDOWN_SECONDS)
                log.exception("V123_WS connect failed")

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

                with self._lock:
                    next_connect_at = self._next_connect_at
                    active = self._active
                if self._tokens and not active:
                    if next_connect_at is None or now >= next_connect_at:
                        self._connect(access_token)
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
