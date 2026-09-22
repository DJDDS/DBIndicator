"""Bounded live microstructure stream for V12.2B stock-option decision support.

A second KiteTicker connection is used only for the small tactical pool chosen
by the existing 15-minute radar.  It is completely separate from the V12.1
NIFTY recorder and from Trial-25 storage.

No orders are placed.  The service emits read-only states such as READY,
TRADEABLE, TIME_EXIT, PROFIT_PROTECT and STALE.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import threading
import time
from collections import defaultdict, deque
from pathlib import Path
from statistics import median
from typing import Callable

from . import derivative_intelligence, scanner, v12_earnings_calendar, v122b_tactical


def _f(v, default=None):
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def _dt(v):
    if isinstance(v, dt.datetime):
        return v
    if not v:
        return None
    try:
        return dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _ticker_factory(api_key, access_token):
    from kiteconnect import KiteTicker
    return KiteTicker(api_key, access_token)


def _reactor_getter():
    try:
        from twisted.internet import reactor
        return reactor
    except Exception:
        return None


def _market_open(now):
    if now.weekday() >= 5:
        return False
    minute = now.hour * 60 + now.minute
    return 9 * 60 + 15 <= minute <= 15 * 60 + 30


def _iso(now):
    return now.isoformat(timespec="seconds")


def _atomic_json(path, payload):
    if not path:
        return
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, default=str, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


def _append_jsonl(path, payload):
    if not path:
        return
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, default=str, sort_keys=True, separators=(",", ":")) + "\n")


class TacticalStockStreamService:
    def __init__(
        self,
        *,
        candidate_provider: Callable[[], list[dict]],
        publish_callback: Callable[[dict], None],
        access_token_getter,
        kite_client_getter,
        api_key: str,
        earnings_state_file,
        state_file,
        event_file,
        ticker_factory=None,
        now_provider=None,
        sleep_fn=time.sleep,
        reactor_getter=None,
        stale_seconds=v122b_tactical.PROPOSED_STALE_SECONDS,
    ):
        self.candidate_provider = candidate_provider
        self.publish_callback = publish_callback
        self.access_token_getter = access_token_getter
        self.kite_client_getter = kite_client_getter
        self.api_key = api_key
        self.earnings_state_file = earnings_state_file
        self.state_file = state_file
        self.event_file = event_file
        self.ticker_factory = ticker_factory or _ticker_factory
        self.now_provider = now_provider or scanner.now_ist
        self.sleep_fn = sleep_fn
        self.reactor_getter = reactor_getter or _reactor_getter
        self.stale_seconds = max(3.0, float(stale_seconds))

        self._lock = threading.RLock()
        self._ticker = None
        self._active = False
        self._connected = False
        self._subscribed = set()
        self._metadata = {}
        self._candidates = {}
        self._latest_ticks = {}
        self._bar_builders = {}
        self._bars = defaultdict(lambda: deque(maxlen=240))
        self._tod_baseline = defaultdict(dict)
        self._depth_samples = defaultdict(lambda: deque(maxlen=180))
        self._basis_samples = defaultdict(lambda: deque(maxlen=180))
        self._lifecycle = {}
        self._last_states = {}
        self._snapshot = {
            "status": "WAITING",
            "validation_label": "INTERIM / NOT VALIDATED",
            "candidates": [],
            "counts": {},
        }
        self._seeded = set()
        self._universe_signature = None
        self._last_tick_at = None

    def snapshot(self):
        with self._lock:
            return json.loads(json.dumps(self._snapshot, default=str))

    def _publish(self, payload):
        payload = dict(payload)
        payload["validation_label"] = "INTERIM / NOT VALIDATED"
        payload["updated_at"] = _iso(self.now_provider())
        with self._lock:
            self._snapshot = payload
        try:
            _atomic_json(self.state_file, payload)
        except OSError:
            pass
        try:
            self.publish_callback(payload)
        except Exception:
            pass

    def _dispatch(self, fn):
        reactor = self.reactor_getter()
        if reactor is not None and getattr(reactor, "running", False):
            reactor.callFromThread(fn)
        else:
            fn()

    def _candidate_signature(self, candidates):
        return tuple(sorted((str(x.get("symbol")), str(x.get("direction"))) for x in candidates if x.get("symbol")))

    def _seed_three_minute(self, kite, symbol, token, now):
        if symbol in self._seeded:
            return
        try:
            start = now - dt.timedelta(days=12)
            rows = kite.historical_data(int(token), start, now, "3minute") or []
        except Exception:
            rows = []
        bars = []
        volumes_by_slot = defaultdict(list)
        today = now.date()
        for row in rows:
            ts = row.get("date")
            if not isinstance(ts, dt.datetime):
                ts = _dt(ts)
            if ts is None:
                continue
            bar = {
                "ts": ts.replace(tzinfo=None).isoformat(timespec="seconds"),
                "open": _f(row.get("open")), "high": _f(row.get("high")),
                "low": _f(row.get("low")), "close": _f(row.get("close")),
                "volume": _f(row.get("volume"), 0.0), "complete": True,
            }
            if None in (bar["open"], bar["high"], bar["low"], bar["close"]):
                continue
            if ts.date() < today:
                slot = f"{ts.hour:02d}:{(ts.minute // 3) * 3:02d}"
                volumes_by_slot[slot].append(float(bar["volume"] or 0.0))
            bars.append(bar)
        with self._lock:
            for bar in bars[-220:]:
                self._bars[symbol].append(bar)
            self._tod_baseline[symbol] = {
                slot: median(vals) for slot, vals in volumes_by_slot.items() if vals
            }
            self._seeded.add(symbol)
        # Bounded pool => at most eight one-off historical calls.  Keep below
        # Kite's historical request rate rather than bursting them.
        self.sleep_fn(0.38)

    def _resolve_universe(self, kite, candidates, now):
        cash_map = scanner.cached_nse_instrument_tokens([x.get("symbol") for x in candidates])
        fut_map = scanner.get_futures_contracts_map(kite)
        opt_map = derivative_intelligence.get_option_contracts_map(kite)
        metadata = {}
        for candidate in candidates:
            symbol = str(candidate.get("symbol") or "")
            direction = candidate.get("direction")
            spot = _f(candidate.get("close"))
            cash_token = cash_map.get(symbol)
            if cash_token:
                metadata[int(cash_token)] = {"kind": "CASH", "symbol": symbol, "tradingsymbol": symbol}
                self._seed_three_minute(kite, symbol, cash_token, now)

            futures = list(fut_map.get(symbol) or [])
            if futures:
                fut = futures[0]
                tok = fut.get("instrument_token")
                if tok:
                    metadata[int(tok)] = {
                        "kind": "FUTURE", "symbol": symbol,
                        "tradingsymbol": fut.get("tradingsymbol"),
                        "expiry": fut.get("expiry"),
                    }

            typ = "CE" if direction == "Bullish" else "PE"
            contracts = [x for x in list(opt_map.get(symbol) or []) if x.get("instrument_type") == typ and x.get("expiry") and x.get("expiry") >= now.date()]
            expiries = sorted({x.get("expiry") for x in contracts})[:2]
            for expiry in expiries:
                expc = [x for x in contracts if x.get("expiry") == expiry and _f(x.get("strike")) is not None]
                if not expc or spot is None:
                    continue
                strikes = sorted({_f(x.get("strike")) for x in expc})
                atm = min(strikes, key=lambda s: abs(s - spot))
                ix = strikes.index(atm)
                if direction == "Bullish":
                    keep = set(strikes[max(0, ix - 1):ix + 1])
                else:
                    keep = set(strikes[ix:min(len(strikes), ix + 2)])
                for con in expc:
                    if _f(con.get("strike")) not in keep:
                        continue
                    tok = con.get("instrument_token")
                    if tok:
                        metadata[int(tok)] = {
                            "kind": "OPTION", "symbol": symbol,
                            "tradingsymbol": con.get("tradingsymbol"),
                            "instrument_token": int(tok),
                            "instrument_type": con.get("instrument_type"),
                            "strike": con.get("strike"), "expiry": con.get("expiry"),
                            "lot_size": con.get("lot_size"),
                        }

        # Subscribe to NIFTY spot as a light 3m relative-move witness.
        try:
            nifty = scanner.get_index_token(kite, "NIFTY 50")
        except Exception:
            nifty = None
        if nifty:
            metadata[int(nifty)] = {"kind": "INDEX", "symbol": "NIFTY 50", "tradingsymbol": "NIFTY 50"}
        return metadata

    def _apply_subscription(self, metadata):
        tokens = set(metadata)
        with self._lock:
            ticker = self._ticker
            connected = self._connected
            old = set(self._subscribed)
            self._metadata = dict(metadata)
        if ticker is None or not connected:
            return

        add = sorted(tokens - old)
        remove = sorted(old - tokens)

        def update():
            try:
                if remove:
                    ticker.unsubscribe(remove)
                if add:
                    ticker.subscribe(add)
                    ticker.set_mode(ticker.MODE_FULL, add)
                with self._lock:
                    self._subscribed = tokens
            except Exception:
                pass
        self._dispatch(update)

    def _refresh_candidates(self, kite, now):
        candidates = list(self.candidate_provider() or [])[:v122b_tactical.TACTICAL_POOL_MAX]
        sig = self._candidate_signature(candidates)
        with self._lock:
            prior = self._universe_signature
        if sig == prior:
            return
        metadata = self._resolve_universe(kite, candidates, now)
        with self._lock:
            self._candidates = {str(x.get("symbol")): dict(x) for x in candidates if x.get("symbol")}
            self._universe_signature = sig
        self._apply_subscription(metadata)
        self._evaluate(now)

    def _tick_age(self, tick, now):
        ts = _dt((tick or {}).get("_received_at"))
        if ts is None:
            return None
        if ts.tzinfo is not None and now.tzinfo is None:
            ts = ts.replace(tzinfo=None)
        return max(0.0, (now - ts).total_seconds())

    def _option_snapshots(self, symbol, spot, now):
        out = []
        with self._lock:
            pairs = [
                (dict(meta), dict(self._latest_ticks.get(tok) or {}))
                for tok, meta in self._metadata.items()
                if meta.get("kind") == "OPTION" and meta.get("symbol") == symbol
            ]
        for meta, tick in pairs:
            try:
                snap = derivative_intelligence.contract_snapshot(meta, tick, spot, now)
            except Exception:
                snap = None
            if snap:
                snap["lot_size"] = meta.get("lot_size")
                out.append(snap)
        return out

    def _basis(self, symbol, now):
        with self._lock:
            cash = next((self._latest_ticks.get(t) for t, m in self._metadata.items() if m.get("kind") == "CASH" and m.get("symbol") == symbol), None)
            fut = next((self._latest_ticks.get(t) for t, m in self._metadata.items() if m.get("kind") == "FUTURE" and m.get("symbol") == symbol), None)
        current = v122b_tactical.synchronized_basis(cash, fut)
        hist = self._basis_samples[symbol]
        if current.get("valid"):
            hist.append({"ts": now, "basis_pct": current.get("basis_pct")})
        cutoff = now - dt.timedelta(seconds=60)
        while hist and hist[0]["ts"] < cutoff:
            hist.popleft()
        accel = None
        if len(hist) >= 2:
            accel = _f(hist[-1].get("basis_pct"), 0.0) - _f(hist[0].get("basis_pct"), 0.0)
        current["basis_change_60s_pct_points"] = round(accel, 6) if accel is not None else None
        return current

    def _relative_3m(self, symbol):
        with self._lock:
            sb = list(self._bars.get(symbol) or [])
            nb = list(self._bars.get("NIFTY 50") or [])
        if not sb or not nb:
            return None
        s0, s1 = _f(sb[-1].get("open")), _f(sb[-1].get("close"))
        n0, n1 = _f(nb[-1].get("open")), _f(nb[-1].get("close"))
        if None in (s0, s1, n0, n1) or s0 <= 0 or n0 <= 0:
            return None
        return round((s1 / s0 - 1.0) * 100.0 - (n1 / n0 - 1.0) * 100.0, 4)

    def _manage_lifecycle(self, symbol, row, setup, state, now):
        life = self._lifecycle.setdefault(symbol, {})
        direction = setup.get("direction") or row.get("direction")
        sign = 1 if direction == "Bullish" else -1
        price = _f(row.get("live_price"), _f(row.get("close")))
        invalidation = _f(setup.get("invalidation"))
        if price is None:
            return state, life

        if state.get("state") == "TRADEABLE" and life.get("triggered_at") is None:
            life.update({
                "triggered_at": now,
                "entry_underlying": price,
                "best_favourable": 0.0,
                "setup": setup.get("setup"),
                "speed_class": setup.get("speed_class"),
            })

        trig = life.get("triggered_at")
        if trig is None:
            return state, life

        entry = _f(life.get("entry_underlying"), price)
        favourable = sign * (price - entry)
        life["best_favourable"] = max(_f(life.get("best_favourable"), 0.0), favourable)

        if invalidation is not None and sign * (price - invalidation) <= 0:
            return {"state": "EXIT", "tradeable": False, "reason": "underlying structural invalidation hit"}, life

        expected = max(_f(setup.get("expected_move_abs"), 0.0), 1e-9)
        bars_allowed = v122b_tactical.PROPOSED_FOLLOWTHROUGH_BARS.get(setup.get("speed_class"))
        if bars_allowed:
            elapsed_bars = int(max(0.0, (now - trig).total_seconds()) // 180)
            if elapsed_bars >= bars_allowed and _f(life.get("best_favourable"), 0.0) < v122b_tactical.PROPOSED_MIN_FOLLOWTHROUGH_FRACTION * expected:
                return {"state": "TIME_EXIT", "tradeable": False, "reason": "setup failed its speed-class follow-through clock"}, life

        if _f(life.get("best_favourable"), 0.0) >= v122b_tactical.PROPOSED_PROFIT_PROTECT_FRACTION * expected:
            bars = list(self._bars.get(symbol) or [])
            trail = None
            if len(bars) >= 2:
                if sign > 0:
                    trail = min(_f(b.get("low"), price) for b in bars[-2:])
                else:
                    trail = max(_f(b.get("high"), price) for b in bars[-2:])
            life["trailing_invalidation"] = trail
            if trail is not None and sign * (price - trail) <= 0:
                return {"state": "EXIT", "tradeable": False, "reason": "3m profit-protection structure lost"}, life
            return {"state": "PROFIT_PROTECT", "tradeable": True, "reason": "move proved itself; protect with fast 3m trailing structure"}, life

        return state, life

    def _log_transition(self, symbol, old, new, payload):
        if old == new:
            return
        record = {
            "ts": _iso(self.now_provider()), "symbol": symbol,
            "from_state": old, "to_state": new,
            "setup": payload.get("setup"), "direction": payload.get("direction"),
            "trigger": payload.get("trigger"), "invalidation": payload.get("invalidation"),
            "option_contract": ((payload.get("option_route") or {}).get("contract") or {}).get("symbol"),
            "underlying": payload.get("live_price"),
            "validation_label": "INTERIM / NOT VALIDATED",
        }
        try:
            _append_jsonl(self.event_file, record)
        except OSError:
            pass

    def _evaluate(self, now):
        with self._lock:
            candidates = [dict(x) for x in self._candidates.values()]
            metadata = dict(self._metadata)
            ticks = dict(self._latest_ticks)

        earnings_state = v12_earnings_calendar._load_state(self.earnings_state_file)
        output = []
        direction_used = defaultdict(int)
        for candidate in candidates:
            symbol = str(candidate.get("symbol"))
            direction = candidate.get("direction")
            cash_tok = next((t for t, m in metadata.items() if m.get("kind") == "CASH" and m.get("symbol") == symbol), None)
            fut_tok = next((t for t, m in metadata.items() if m.get("kind") == "FUTURE" and m.get("symbol") == symbol), None)
            cash_tick = ticks.get(cash_tok) or {}
            fut_tick = ticks.get(fut_tok) or {}
            live_price = _f(cash_tick.get("last_price"), _f(candidate.get("close")))
            candidate["live_price"] = live_price

            with self._lock:
                bars = list(self._bars.get(symbol) or [])
                builder = self._bar_builders.get(symbol)
                current = dict(builder.current) if builder is not None and builder.current else None
                depth_samples = list(self._depth_samples.get(symbol) or [])

            setup = v122b_tactical.detect_structural_setup(bars, current, candidate, now=now)
            fast = v122b_tactical.fast_trend_veto((bars + ([current] if current else []))[-80:], setup.get("direction") or direction)
            persistence = v122b_tactical.depth_persistence(depth_samples, setup.get("direction") or direction, now=now)
            cash_age = self._tick_age(cash_tick, now)
            fut_age = self._tick_age(fut_tick, now)
            stale = cash_age is None or cash_age > self.stale_seconds or fut_age is None or fut_age > self.stale_seconds
            event = v122b_tactical.earnings_context(earnings_state, symbol, now)
            option_snaps = self._option_snapshots(symbol, live_price or _f(candidate.get("close"), 0.0), now)
            route = None
            if setup.get("setup") and live_price:
                route = v122b_tactical.route_option(
                    option_snaps,
                    direction=setup.get("direction") or direction,
                    spot=live_price,
                    now=now,
                    speed_class=setup.get("speed_class") or "IMPULSE",
                    expected_underlying_move_abs=max(_f(setup.get("expected_move_abs"), 0.0), 0.25 * max(_f(candidate.get("atr"), 0.0), 0.0)),
                    earnings=event,
                )

            state = v122b_tactical.classify_state(
                setup,
                fast_veto=fast,
                stale=stale,
                depth_persist=persistence,
                option_route=route,
                active_same_direction=direction_used[setup.get("direction") or direction],
            )
            if state.get("state") == "TRADEABLE":
                direction_used[setup.get("direction") or direction] += 1

            state, life = self._manage_lifecycle(symbol, candidate, setup, state, now)
            basis = self._basis(symbol, now)
            slot = f"{now.hour:02d}:{(now.minute // 3) * 3:02d}"
            baseline = self._tod_baseline.get(symbol, {}).get(slot)
            rvol3 = v122b_tactical.projected_three_minute_rvol(current, baseline, now)
            option_route = route or {}
            risk = v122b_tactical.one_lot_risk_preview(
                option_route.get("contract"),
                spot=live_price,
                invalidation=setup.get("invalidation"),
            )
            payload = {
                "symbol": symbol,
                "direction": setup.get("direction") or direction,
                "state": state.get("state"),
                "reason": state.get("reason"),
                "tradeable": bool(state.get("tradeable")),
                "setup": setup.get("setup"),
                "speed_class": setup.get("speed_class"),
                "trigger": setup.get("trigger"),
                "invalidation": setup.get("invalidation"),
                "expected_move_abs": setup.get("expected_move_abs"),
                "live_price": live_price,
                "candidate_phase": candidate.get("phase"),
                "candidate_pressure": candidate.get("pressure"),
                "candidate_runway": candidate.get("runway"),
                "cash_tick_age_s": round(cash_age, 2) if cash_age is not None else None,
                "future_tick_age_s": round(fut_age, 2) if fut_age is not None else None,
                "depth": persistence,
                "basis": basis,
                "rvol_3m": rvol3,
                "relative_3m_vs_nifty_pct": self._relative_3m(symbol),
                "fast_veto": fast,
                "earnings": event,
                "option_route": option_route,
                "one_lot_risk": risk,
                "triggered_at": _iso(life.get("triggered_at")) if isinstance(life.get("triggered_at"), dt.datetime) else None,
                "best_favourable_abs": _f(life.get("best_favourable")),
                "trailing_invalidation": _f(life.get("trailing_invalidation")),
                "signal_age_seconds": round((now - life["triggered_at"]).total_seconds(), 1) if isinstance(life.get("triggered_at"), dt.datetime) else None,
            }
            old = self._last_states.get(symbol)
            self._log_transition(symbol, old, payload["state"], payload)
            self._last_states[symbol] = payload["state"]
            output.append(payload)

        priority = {
            "TRADEABLE": 10, "PROFIT_PROTECT": 9, "READY": 8, "TRIGGERED": 7,
            "FORMING": 6, "OPTION_NOT_TRADEABLE": 5, "TIME_EXIT": 4,
            "CANCELLED": 3, "BLOCKED_EXPOSURE": 2, "RESEARCH_ONLY": 1,
            "STALE": 0, "EXIT": 0,
        }
        output.sort(key=lambda x: (priority.get(x.get("state"), 0), _f(x.get("candidate_pressure"), 0.0)), reverse=True)
        counts = {}
        for row in output:
            counts[row["state"]] = counts.get(row["state"], 0) + 1
        self._publish({
            "status": "STREAMING" if self._connected else ("WAITING_CANDIDATES" if not candidates else "CONNECTING"),
            "candidate_count": len(output),
            "candidates": output,
            "counts": counts,
            "last_tick_at": _iso(self._last_tick_at) if isinstance(self._last_tick_at, dt.datetime) else None,
            "rules": {
                "pool_max": v122b_tactical.TACTICAL_POOL_MAX,
                "pre_result_next_month_dte_lte": v122b_tactical.PROPOSED_PRE_RESULT_NEXT_MONTH_DTE,
                "max_friction_ratio": v122b_tactical.PROPOSED_MAX_FRICTION_TO_EXPECTED_MOVE,
                "same_direction_cap": v122b_tactical.PROPOSED_MAX_SAME_DIRECTION_ACTIVE,
            },
        })

    def _handle_ticks(self, ticks, now):
        with self._lock:
            metadata = dict(self._metadata)
        for raw in ticks or []:
            tok = raw.get("instrument_token")
            if tok is None:
                continue
            tok = int(tok)
            meta = metadata.get(tok)
            if not meta:
                continue
            tick = dict(raw)
            tick["_received_at"] = _iso(now)
            with self._lock:
                self._latest_ticks[tok] = tick
                self._last_tick_at = now

            kind = meta.get("kind")
            symbol = meta.get("symbol")
            if kind in ("CASH", "INDEX"):
                with self._lock:
                    builder = self._bar_builders.get(symbol)
                    if builder is None:
                        builder = v122b_tactical.ThreeMinuteBarBuilder()
                        self._bar_builders[symbol] = builder
                    completed = builder.update(tick.get("last_price"), tick.get("volume_traded", tick.get("volume")), now)
                    if completed:
                        self._bars[symbol].append(completed)
            elif kind == "FUTURE":
                metrics = v122b_tactical.weighted_depth_metrics(tick)
                metrics["ts"] = _iso(now)
                with self._lock:
                    self._depth_samples[symbol].append(metrics)
                    cutoff = now - dt.timedelta(seconds=90)
                    while self._depth_samples[symbol]:
                        ts = _dt(self._depth_samples[symbol][0].get("ts"))
                        if ts is None or ts >= cutoff:
                            break
                        self._depth_samples[symbol].popleft()

        self._evaluate(now)

    def _connect(self, token):
        ticker = self.ticker_factory(self.api_key, token)
        with self._lock:
            self._ticker = ticker
            self._active = True
            metadata = dict(self._metadata)

        def on_connect(ws, response):
            with self._lock:
                if ws is not self._ticker:
                    return
                tokens = sorted(self._metadata)
            if tokens:
                ws.subscribe(tokens)
                ws.set_mode(ws.MODE_FULL, tokens)
            with self._lock:
                self._subscribed = set(tokens)
                self._connected = True
            self._evaluate(self.now_provider())

        def on_ticks(ws, ticks):
            with self._lock:
                if ws is not self._ticker:
                    return
            self._handle_ticks(ticks, self.now_provider())

        def on_close(ws, code, reason):
            with self._lock:
                if ws is self._ticker:
                    self._connected = False
            self._publish({**self.snapshot(), "status": "STALE", "last_error": str(reason or "websocket closed")})

        def on_error(ws, code, reason):
            self._publish({**self.snapshot(), "status": "ERROR", "last_error": str(reason or code)})

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
                    self._ticker = None
                    self._connected = False
                self._publish({**self.snapshot(), "status": "ERROR", "last_error": str(exc)})
        self._dispatch(connect)

    def _close(self):
        with self._lock:
            ticker = self._ticker
            self._ticker = None
            self._active = False
            self._connected = False
            self._subscribed = set()
        if ticker is not None:
            self._dispatch(lambda: ticker.close())

    def run_forever(self):
        while True:
            now = self.now_provider()
            try:
                if not _market_open(now):
                    if self._active:
                        self._close()
                    self._publish({
                        "status": "MARKET_CLOSED", "candidate_count": 0,
                        "candidates": [], "counts": {}, "last_tick_at": None,
                    })
                    self.sleep_fn(30)
                    continue

                kite = self.kite_client_getter()
                token = self.access_token_getter()
                if kite is None or not token:
                    self._publish({"status": "WAITING_LOGIN", "candidate_count": 0, "candidates": [], "counts": {}})
                    self.sleep_fn(10)
                    continue

                self._refresh_candidates(kite, now)
                with self._lock:
                    has_candidates = bool(self._candidates)
                    active = self._active
                if has_candidates and not active:
                    self._connect(token)
                elif not has_candidates:
                    self._publish({"status": "WAITING_CANDIDATES", "candidate_count": 0, "candidates": [], "counts": {}})
                else:
                    self._evaluate(now)
                self.sleep_fn(2)
            except Exception as exc:
                self._publish({**self.snapshot(), "status": "ERROR", "last_error": str(exc)})
                self.sleep_fn(5)
