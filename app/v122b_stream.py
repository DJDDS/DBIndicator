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


SOFT_STATE_DWELL_SECONDS = 20.0
CANCEL_REARM_COOLDOWN_SECONDS = 180.0
STALE_VOID_SECONDS = 60.0
NEW_ENTRY_CUTOFF_SECONDS = 15 * 3600 + 25 * 60


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
    second = now.hour * 3600 + now.minute * 60 + now.second
    return 9 * 3600 + 15 * 60 <= second < 15 * 3600 + 30 * 60


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
        self.continuation_file = (
            str(Path(event_file).with_name("v123_continuation_shadow.jsonl"))
            if event_file else None
        )
        self._shadow_samples_written = 0
        self._shadow_episode_keys = set()
        self._shadow_observation_keys = self._load_shadow_observation_keys()
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
        self._transition_ids = set()
        self._transition_seq = 0
        self._snapshot = {
            "status": "WAITING",
            "validation_label": "INTERIM / NOT VALIDATED",
            "candidates": [],
            "counts": {},
        }
        self._seeded = set()
        self._universe_signature = None
        self._last_tick_at = None
        self._session_date = None

    @staticmethod
    def _shadow_observation_key(row):
        """Deterministic idempotency key for one shadow observation."""
        return "|".join(str(x or "") for x in (
            row.get("trade_date"),
            row.get("symbol"),
            row.get("episode_no"),
            row.get("triggered_at"),
            row.get("sample_kind"),
            row.get("horizon_min"),
            row.get("target_horizon_min"),
        ))

    def _load_shadow_observation_keys(self):
        keys = set()
        if not self.continuation_file:
            return keys
        path = Path(self.continuation_file)
        if not path.exists():
            return keys
        try:
            with path.open("r", encoding="utf-8") as handle:
                for raw in handle:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        row = json.loads(raw)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        continue
                    keys.add(self._shadow_observation_key(row))
        except OSError:
            pass
        return keys

    @staticmethod
    def _reset_trigger(life):
        """Clear episode-specific timing/price state without erasing history."""
        for key in (
            "triggered_at",
            "entry_underlying",
            "best_favourable",
            "trailing_invalidation",
            "setup",
            "speed_class",
            "route_degraded_since",
            "route_blocked_since",
            "last_executable_at",
            "worst_adverse",
            "path_length_abs",
            "last_path_price",
            "shadow_outcome",
            "shadow_outcome_at",
            "shadow_recorded_milestones",
            "shadow_episode_direction",
            "shadow_entry_atr",
            "shadow_entry_atr3",
            "shadow_best_favourable",
            "shadow_worst_adverse",
            "shadow_path_length_abs",
            "shadow_last_path_price",
            "shadow_direction_mismatch_seen",
            "shadow_t0_recorded",
            "shadow_missed_milestones",
            "shadow_barrier_hits",
            "shadow_void_reason",
            "plan_option_contract",
            "plan_option_entry_mid",
            "plan_option_entry_delta",
            "plan_option_entry_gamma",
        ):
            life.pop(key, None)

    def _maybe_reset_session(self, now):
        """Start each trading day with clean tactical market state.

        Focus/observer persistence is handled outside this service.  This reset
        is deliberately scoped to the deep tactical stream so yesterday's
        bars, depth, basis and entry clocks cannot leak into a new session.
        """
        day = now.date()
        with self._lock:
            if self._session_date is None:
                self._session_date = day
                return False
            if self._session_date == day:
                return False
            self._session_date = day
            self._latest_ticks = {}
            self._bar_builders = {}
            self._bars = defaultdict(lambda: deque(maxlen=240))
            self._tod_baseline = defaultdict(dict)
            self._depth_samples = defaultdict(lambda: deque(maxlen=180))
            self._basis_samples = defaultdict(lambda: deque(maxlen=180))
            self._lifecycle = {}
            self._last_states = {}
            self._transition_ids = set()
            self._transition_seq = 0
            self._seeded = set()
            self._universe_signature = None
            self._last_tick_at = None
        return True

    def _drop_symbol_market_state(self, symbol):
        """Discard only stale deep-stream market construction for a removed name.

        If the name later returns to Focus/Continuation it is re-seeded from
        clean history instead of completing an old partial bar after a gap.
        """
        with self._lock:
            self._bar_builders.pop(symbol, None)
            self._bars.pop(symbol, None)
            self._tod_baseline.pop(symbol, None)
            self._depth_samples.pop(symbol, None)
            self._basis_samples.pop(symbol, None)
            self._seeded.discard(symbol)

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
        # Include the latest 15m spot anchor so option strikes are refreshed
        # when a continuing candidate moves materially; historical 3m seeding
        # remains one-shot and therefore this does not multiply REST history.
        return tuple(sorted(
            (
                str(x.get("symbol")), str(x.get("direction")),
                round(_f(x.get("close"), 0.0), 2),
                str(x.get("locked_option_contract") or ""),
            )
            for x in candidates if x.get("symbol")
        ))

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
        now_cmp = now.replace(tzinfo=None) if isinstance(now, dt.datetime) else now
        current_bucket = v122b_tactical.ThreeMinuteBarBuilder.bucket_start(now_cmp)
        for row in rows:
            ts = row.get("date")
            if not isinstance(ts, dt.datetime):
                ts = _dt(ts)
            if ts is None:
                continue
            ts_cmp = ts.replace(tzinfo=None)
            # Kite historical_data can include the still-forming current
            # 3-minute candle.  Never seed that bucket as complete; the live
            # builder owns it.
            if v122b_tactical.ThreeMinuteBarBuilder.bucket_start(ts_cmp) >= current_bucket:
                continue
            bar = {
                "ts": ts_cmp.isoformat(timespec="seconds"),
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
                # Focus names get a small but complete local neighborhood:
                # one ITM + ATM + one OTM.  The prior two-strike window could
                # make the router jump merely because the stock moved one
                # strike interval.
                keep = set(strikes[max(0, ix - 1):min(len(strikes), ix + 2)])
                locked_symbol = str(candidate.get("locked_option_contract") or "")
                for con in expc:
                    if _f(con.get("strike")) not in keep and str(con.get("tradingsymbol") or "") != locked_symbol:
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
        new_symbols = {str(x.get("symbol")) for x in candidates if x.get("symbol")}
        with self._lock:
            prior = self._universe_signature
            old_symbols = set(self._candidates)
        if sig == prior:
            return
        for symbol in old_symbols - new_symbols:
            self._drop_symbol_market_state(symbol)
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

    def _rvol_3m(self, symbol, current, now):
        slot = f"{now.hour:02d}:{(now.minute // 3) * 3:02d}"
        baseline = self._tod_baseline.get(symbol, {}).get(slot)
        current_rvol = v122b_tactical.projected_three_minute_rvol(current, baseline, now)

        previous_rvol = None
        with self._lock:
            bars = list(self._bars.get(symbol) or [])
        if bars:
            prev = bars[-1]
            pts = _dt(prev.get("ts"))
            if pts is not None:
                pslot = f"{pts.hour:02d}:{(pts.minute // 3) * 3:02d}"
                pbase = _f(self._tod_baseline.get(symbol, {}).get(pslot))
                pvol = _f(prev.get("volume"))
                if pbase is not None and pbase > 0 and pvol is not None:
                    previous_rvol = pvol / pbase

        accel = None
        if current_rvol is not None and previous_rvol is not None:
            accel = current_rvol - previous_rvol
        return (
            current_rvol,
            round(accel, 3) if accel is not None else None,
            round(previous_rvol, 3) if previous_rvol is not None else None,
        )


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

    @staticmethod
    def _episode_signature(setup, candidate):
        """Stable description of the market structure that opened an episode."""
        return {
            "direction": str(setup.get("direction") or candidate.get("direction") or ""),
            "setup": str(setup.get("setup") or ""),
            "trigger": _f(setup.get("trigger")),
            "event_family": str(candidate.get("focus_event_family") or ""),
        }

    @staticmethod
    def _session_allows_new_entry(now):
        second = now.hour * 3600 + now.minute * 60 + now.second
        return second < NEW_ENTRY_CUTOFF_SECONDS

    @staticmethod
    def _cancel_cooldown_allowed(life, setup, candidate, live_price, now):
        cancelled_at = life.get("last_cancelled_at")
        if not isinstance(cancelled_at, dt.datetime):
            return True, "NO_CANCEL_COOLDOWN"
        age = max(0.0, (now - cancelled_at).total_seconds())
        if age >= CANCEL_REARM_COOLDOWN_SECONDS:
            return True, "CANCEL_COOLDOWN_EXPIRED"
        direction = str(setup.get("direction") or candidate.get("direction") or "")
        if direction and direction != str(life.get("last_cancelled_direction") or direction):
            return True, "DIRECTION_CHANGED"
        atr = _f(life.get("last_cancelled_atr"), _f(candidate.get("atr")))
        cancel_price = _f(life.get("last_cancelled_price"))
        if atr and atr > 0 and live_price is not None and cancel_price is not None:
            if abs(float(live_price) - cancel_price) / atr >= 0.20:
                return True, "PRICE_RESET_GE_0_20_ATR"
        return False, f"CANCEL_COOLDOWN_{int(CANCEL_REARM_COOLDOWN_SECONDS - age)}S_REMAIN"

    def _stabilize_tactical_state(self, life, raw_state, *, now, stale, live_price, candidate):
        """Hold feed gaps and soft deteriorations without delaying hard exits."""
        raw_state = dict(raw_state or {})
        prior = str(life.get("last_valid_tactical_state") or "")

        if stale:
            if not isinstance(life.get("stale_since"), dt.datetime):
                life["stale_since"] = now
            stale_for = max(0.0, (now - life["stale_since"]).total_seconds())
            if stale_for >= STALE_VOID_SECONDS:
                life["shadow_void_reason"] = "STALE_GT_60S"
            held = prior or "FORMING"
            return {
                "state": held,
                "tradeable": False,
                "reason": "DATA HOLD — deep tactical feed stale; last valid state retained",
                "data_ok": False,
                "data_status": "STALE",
                "stale_seconds": round(stale_for, 1),
                "held_state": held,
                "execution_paused": True,
            }

        life.pop("stale_since", None)
        raw_state["data_ok"] = True
        raw_state["data_status"] = "LIVE"
        state_name = str(raw_state.get("state") or "FORMING")
        reason = str(raw_state.get("reason") or "")

        # Hard events are immediate: structural/time exits, fast-veto cancels,
        # hard route blocks and profit-protection management must not wait.
        hard = state_name in ("EXIT", "TIME_EXIT", "PROFIT_PROTECT", "BLOCKED_EXPOSURE")
        if state_name == "CANCELLED" and "persistent futures depth opposes" not in reason.lower():
            hard = True
        if state_name in ("OPTION_NOT_TRADEABLE",) and "no valid option expiry" in reason.lower():
            hard = True

        soft_deterioration = bool(
            prior in ("READY", "TRIGGERED", "TRADEABLE", "PROFIT_PROTECT")
            and state_name in ("FORMING", "CANCELLED", "OPTION_NOT_TRADEABLE", "ROUTE_DEGRADED")
            and not hard
        )

        if soft_deterioration:
            pending_key = state_name + "|" + reason
            if life.get("soft_pending_key") != pending_key:
                life["soft_pending_key"] = pending_key
                life["soft_pending_since"] = now
            pending_since = life.get("soft_pending_since")
            elapsed = (now - pending_since).total_seconds() if isinstance(pending_since, dt.datetime) else 0.0
            if elapsed < SOFT_STATE_DWELL_SECONDS:
                held = prior or state_name
                return {
                    "state": held,
                    "tradeable": False,
                    "reason": f"EXECUTION PAUSE — {reason or state_name}; confirming for {SOFT_STATE_DWELL_SECONDS:.0f}s",
                    "data_ok": True,
                    "data_status": "LIVE",
                    "soft_hold": True,
                    "soft_pending_state": state_name,
                    "soft_pending_seconds": round(elapsed, 1),
                    "execution_paused": True,
                }
        else:
            life.pop("soft_pending_key", None)
            life.pop("soft_pending_since", None)

        # Accept the raw state after any required dwell.
        life["last_valid_tactical_state"] = state_name
        life["last_valid_tactical_reason"] = reason
        life.pop("soft_pending_key", None)
        life.pop("soft_pending_since", None)
        if state_name == "CANCELLED":
            life["last_cancelled_at"] = now
            life["last_cancelled_price"] = _f(live_price)
            life["last_cancelled_direction"] = str(candidate.get("direction") or "")
            life["last_cancelled_atr"] = _f(candidate.get("atr"))
        return raw_state

    def _fresh_episode_allowed(self, life, setup, candidate, live_price, now):
        """Require genuinely new market information after an episode closes.

        This deliberately reuses the already deployed V12.3 continuation/re-arm
        mathematics rather than inventing another threshold family:
          * pullback-reclaim,
          * event-family change,
          * >=0.20 ATR renewed directional move,
          * >=0.15 ATR structural-trigger shift,
          * renewed 5m relative acceleration.
        """
        cooldown_ok, cooldown_reason = self._cancel_cooldown_allowed(
            life, setup, candidate, live_price, now
        )
        if not cooldown_ok:
            return False, cooldown_reason

        prior = life.get("last_closed_signature")
        if not prior:
            return True, "FIRST_EPISODE"

        closed_at = life.get("last_closed_at")
        rearmed_at = _dt(candidate.get("focus_rearmed_at"))
        if isinstance(closed_at, dt.datetime) and rearmed_at is not None:
            if rearmed_at.tzinfo is not None and closed_at.tzinfo is None:
                rearmed_at = rearmed_at.replace(tzinfo=None)
            elif rearmed_at.tzinfo is None and closed_at.tzinfo is not None:
                rearmed_at = rearmed_at.replace(tzinfo=closed_at.tzinfo)
            if rearmed_at > closed_at:
                return True, str(candidate.get("focus_rearm_reason") or "FOCUS_REARM")

        current = self._episode_signature(setup, candidate)
        family = str(current.get("event_family") or "")
        prior_family = str(prior.get("event_family") or "")
        if family == "PULLBACK_RECLAIM" and prior.get("setup") != "PULLBACK_RECLAIM":
            return True, "FRESH_PULLBACK_RECLAIM"
        if family and prior_family and family != prior_family:
            return True, "EVENT_FAMILY_CHANGED"

        atr = _f(candidate.get("atr"))
        direction = str(current.get("direction") or "")
        sign = 1.0 if direction == "Bullish" else -1.0
        ref_price = _f(life.get("last_closed_price"))
        if atr and atr > 0 and live_price is not None and ref_price is not None:
            if sign * (live_price - ref_price) / atr >= 0.20:
                return True, "RENEWED_MOVE_GE_0_20_ATR"

        current_trigger = _f(current.get("trigger"))
        prior_trigger = _f(prior.get("trigger"))
        if atr and atr > 0 and current_trigger is not None and prior_trigger is not None:
            if abs(current_trigger - prior_trigger) / atr >= 0.15:
                return True, "NEW_STRUCTURAL_TRIGGER_GE_0_15_ATR"

        relative = _f(candidate.get("relative_5m_vs_nifty_pct"))
        ret5 = _f(candidate.get("ret_5m_pct"))
        if relative is not None and ret5 is not None:
            if sign * relative >= 0.25 and sign * ret5 >= 0.20:
                prior_rel = _f(life.get("last_closed_relative_5m"))
                prior_ret5 = _f(life.get("last_closed_ret_5m"))
                # "Renewed" means the existing V12.3 relative-acceleration
                # condition has newly crossed from not-qualified to qualified.
                # Do not invent an extra +0.05 tuning constant merely to
                # manufacture freshness.
                was_already_qualified = (
                    prior_rel is not None and prior_ret5 is not None
                    and sign * prior_rel >= 0.25 and sign * prior_ret5 >= 0.20
                )
                if not was_already_qualified:
                    return True, "RENEWED_5M_RELATIVE_ACCELERATION"

        return False, "WAITING_FOR_FRESH_STRUCTURE_AFTER_PRIOR_EPISODE"

    @staticmethod
    def _continuation_math(life, row, price):
        trig = life.get("triggered_at")
        entry = _f(life.get("entry_underlying"))
        entry_atr = _f(life.get("shadow_entry_atr"), _f(row.get("atr")))
        current_atr = _f(row.get("atr"))
        if not isinstance(trig, dt.datetime) or entry is None or price is None or not entry_atr or entry_atr <= 0:
            return None
        direction = str(life.get("shadow_episode_direction") or row.get("direction") or "")
        sign = 1.0 if direction == "Bullish" else -1.0
        progress_abs = sign * (price - entry)
        path = max(_f(life.get("shadow_path_length_abs"), _f(life.get("path_length_abs"), 0.0)), 0.0)
        mfe = max(_f(life.get("shadow_best_favourable"), _f(life.get("best_favourable"), 0.0)), 0.0)
        mae = max(_f(life.get("shadow_worst_adverse"), _f(life.get("worst_adverse"), 0.0)), 0.0)
        efficiency = progress_abs / path if path > 1e-9 else 0.0
        efficiency = max(-1.0, min(1.0, efficiency))
        pullback = None
        if mfe > 1e-9:
            pullback = max(0.0, min(3.0, (mfe - progress_abs) / mfe))
        out = {
            "progress_atr": round(progress_abs / entry_atr, 4),
            "mfe_atr": round(mfe / entry_atr, 4),
            "mae_atr": round(mae / entry_atr, 4),
            "entry_atr": round(entry_atr, 6),
            "current_atr": round(current_atr, 6) if current_atr is not None else None,
            "path_efficiency": round(efficiency, 4),
            "pullback_ratio": round(pullback, 4) if pullback is not None else None,
            "outcome_first_hit": life.get("shadow_outcome"),
        }
        if current_atr and current_atr > 0:
            out.update({
                "progress_current_atr": round(progress_abs / current_atr, 4),
                "mfe_current_atr": round(mfe / current_atr, 4),
                "mae_current_atr": round(mae / current_atr, 4),
            })
        return out

    def _record_continuation_shadow(self, *, now, symbol, candidate, setup, state, life,
                                    live_price, route_health, route, persistence, fast,
                                    stale, rvol3, rvol3_accel, relative3, five_minute,
                                    cash_age, fut_age, force_close=False):
        """Write research-only post-trigger path snapshots. Never controls trading."""
        metrics = self._continuation_math(life, candidate, live_price)
        trig = life.get("triggered_at")
        if metrics is None or not isinstance(trig, dt.datetime):
            return metrics

        age_s = max(0.0, (now - trig).total_seconds())
        milestones = (180, 360, 600, 900, 1800)
        done = set(life.get("shadow_recorded_milestones") or [])
        due = [m for m in milestones if age_s >= m and m not in done]
        if not due and not force_close:
            return metrics

        option_contract = (route or {}).get("contract") or {}
        atr3_shadow = v122b_tactical.three_minute_atr(list(self._bars.get(symbol) or []))
        entry_atr3_shadow = _f(life.get("shadow_entry_atr3"))
        shadow_mfe = max(_f(life.get("shadow_best_favourable"), _f(life.get("best_favourable"), 0.0)), 0.0)
        shadow_mae = max(_f(life.get("shadow_worst_adverse"), _f(life.get("worst_adverse"), 0.0)), 0.0)
        mfe_atr3_shadow = (
            round(shadow_mfe / entry_atr3_shadow, 4)
            if entry_atr3_shadow else None
        )
        mae_atr3_shadow = (
            round(shadow_mae / entry_atr3_shadow, 4)
            if entry_atr3_shadow else None
        )
        base = {
            "ts": _iso(now),
            "trade_date": now.date().isoformat(),
            "symbol": symbol,
            "direction": life.get("shadow_episode_direction") or setup.get("direction") or candidate.get("direction"),
            "current_candidate_direction": setup.get("direction") or candidate.get("direction"),
            "direction_mismatch_detected": bool(life.get("shadow_direction_mismatch_seen")),
            "setup": setup.get("setup"),
            "event_family": candidate.get("focus_event_family"),
            "episode_no": int(life.get("episode_no") or 0),
            "episode_started_at": _iso(life.get("episode_started_at")) if isinstance(life.get("episode_started_at"), dt.datetime) else None,
            "triggered_at": _iso(trig),
            "trigger": setup.get("trigger"),
            "invalidation": setup.get("invalidation"),
            "entry_underlying": life.get("entry_underlying"),
            "live_price": live_price,
            "atr": candidate.get("atr"),
            "entry_atr": metrics.get("entry_atr"),
            "current_atr": metrics.get("current_atr"),
            **metrics,
            "ret_5m_pct": candidate.get("ret_5m_pct"),
            "relative_5m_vs_nifty_pct": candidate.get("relative_5m_vs_nifty_pct"),
            "relative_3m_vs_nifty_pct": relative3,
            "rvol_3m": rvol3,
            "rvol_3m_accel": rvol3_accel,
            "five_minute_witness": (five_minute or {}).get("state"),
            "route_health": (route_health or {}).get("state"),
            "route_health_reason": (route_health or {}).get("reason"),
            "option_contract": option_contract.get("symbol") or life.get("locked_option_contract"),
            "option_mid": option_contract.get("mid"),
            "option_spread_pct": option_contract.get("spread_pct"),
            "option_delta_abs": option_contract.get("delta_abs") or option_contract.get("delta"),
            "friction_to_expected_move": option_contract.get("friction_to_expected_move"),
            "atr3_14_shadow": atr3_shadow,
            "entry_atr3_shadow": entry_atr3_shadow,
            "current_atr3_14_shadow": atr3_shadow,
            "mfe_atr3_shadow": mfe_atr3_shadow,
            "mae_atr3_shadow": mae_atr3_shadow,
            "mfe_current_atr3_shadow": round(shadow_mfe / atr3_shadow, 4) if atr3_shadow else None,
            "mae_current_atr3_shadow": round(shadow_mae / atr3_shadow, 4) if atr3_shadow else None,
            "depth_support_fraction": (persistence or {}).get("support_fraction"),
            "depth_oppose_fraction": (persistence or {}).get("oppose_fraction"),
            "fast_veto": bool((fast or {}).get("veto")),
            "fast_veto_reason": (fast or {}).get("reason"),
            "stale": bool(stale),
            "cash_tick_age_s": round(cash_age, 2) if cash_age is not None else None,
            "future_tick_age_s": round(fut_age, 2) if fut_age is not None else None,
            "tactical_state": state.get("state"),
            "shadow_only": True,
            "controls_trading": False,
            "validation_label": "SHADOW CONTINUATION MATH / NOT A TRADE FILTER",
        }

        rows = []
        if due:
            milestone = max(due)
            lag_s = max(0.0, age_s - milestone)
            row = dict(base)
            if lag_s <= 90.0:
                row["sample_kind"] = "MILESTONE"
                row["horizon_min"] = milestone // 60
            else:
                row["sample_kind"] = "LATE_OBSERVATION"
                row["horizon_min"] = None
            row["target_horizon_min"] = milestone // 60
            row["horizon_lag_seconds"] = round(lag_s, 1)
            row["missed_horizons_min"] = [m // 60 for m in due if m != milestone]
            row["age_seconds"] = round(age_s, 1)
            rows.append(row)
            done.update(due)
        if force_close:
            row = dict(base)
            row["sample_kind"] = "EPISODE_CLOSE"
            row["horizon_min"] = None
            row["age_seconds"] = round(age_s, 1)
            row["episode_result"] = life.get("episode_result")
            row["episode_exit_reason"] = life.get("episode_exit_reason")
            rows.append(row)

        for row in rows:
            key = self._shadow_observation_key(row)
            row["observation_id"] = key
            if key in self._shadow_observation_keys:
                continue
            try:
                _append_jsonl(self.continuation_file, row)
                self._shadow_observation_keys.add(key)
                self._shadow_samples_written += 1
            except OSError:
                pass
        life["shadow_recorded_milestones"] = sorted(done)
        if force_close:
            self._shadow_episode_keys.add((now.date().isoformat(), symbol, int(life.get("episode_no") or 0)))
        return metrics

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
                "worst_adverse": 0.0,
                "path_length_abs": 0.0,
                "last_path_price": price,
                "shadow_outcome": None,
                "shadow_outcome_at": None,
                "shadow_recorded_milestones": [],
                "shadow_episode_direction": direction,
                "shadow_entry_atr": _f(row.get("atr")),
                "shadow_entry_atr3": v122b_tactical.three_minute_atr(list(self._bars.get(symbol) or [])),
                "shadow_best_favourable": 0.0,
                "shadow_worst_adverse": 0.0,
                "shadow_path_length_abs": 0.0,
                "shadow_last_path_price": price,
                "shadow_direction_mismatch_seen": False,
                "shadow_t0_recorded": False,
                "shadow_missed_milestones": [],
                "shadow_barrier_hits": {},
                "shadow_void_reason": None,
                "setup": setup.get("setup"),
                "speed_class": setup.get("speed_class"),
            })

        trig = life.get("triggered_at")
        if trig is None:
            return state, life

        entry = _f(life.get("entry_underlying"), price)
        favourable = sign * (price - entry)
        life["best_favourable"] = max(_f(life.get("best_favourable"), 0.0), favourable)
        life["worst_adverse"] = max(_f(life.get("worst_adverse"), 0.0), max(0.0, -favourable))
        prior_price = _f(life.get("last_path_price"), price)
        life["path_length_abs"] = _f(life.get("path_length_abs"), 0.0) + abs(price - prior_price)
        life["last_path_price"] = price

        shadow_direction = str(life.get("shadow_episode_direction") or direction)
        shadow_sign = 1 if shadow_direction == "Bullish" else -1
        if str(direction) != shadow_direction:
            life["shadow_direction_mismatch_seen"] = True
        shadow_entry = _f(life.get("entry_underlying"), price)
        shadow_favourable = shadow_sign * (price - shadow_entry)
        life["shadow_best_favourable"] = max(
            _f(life.get("shadow_best_favourable"), 0.0), shadow_favourable
        )
        life["shadow_worst_adverse"] = max(
            _f(life.get("shadow_worst_adverse"), 0.0), max(0.0, -shadow_favourable)
        )
        shadow_prior = _f(life.get("shadow_last_path_price"), price)
        life["shadow_path_length_abs"] = (
            _f(life.get("shadow_path_length_abs"), 0.0) + abs(price - shadow_prior)
        )
        life["shadow_last_path_price"] = price

        # Pre-registered research barrier grid.  It records first touch order
        # only; it never controls the live tactical state.
        shadow_atr0 = _f(life.get("shadow_entry_atr"))
        if shadow_atr0 and shadow_atr0 > 0:
            hits = dict(life.get("shadow_barrier_hits") or {})
            adverse = max(0.0, -shadow_favourable)
            for target_atr in (0.30, 0.55, 0.80):
                for stop_atr in (0.15, 0.35, 0.50):
                    key = f"a{target_atr:.2f}_b{stop_atr:.2f}"
                    if key in hits:
                        continue
                    if shadow_favourable >= target_atr * shadow_atr0:
                        hits[key] = {"first": "TARGET", "ts": _iso(now)}
                    elif adverse >= stop_atr * shadow_atr0:
                        hits[key] = {"first": "STOP", "ts": _iso(now)}
            life["shadow_barrier_hits"] = hits

        atr = _f(row.get("atr"))
        shadow_atr = _f(life.get("shadow_entry_atr"), atr)
        if shadow_atr and shadow_atr > 0 and not life.get("shadow_outcome"):
            if shadow_favourable >= 0.30 * shadow_atr:
                life["shadow_outcome"] = "PLUS_0_30_ATR_FIRST"
                life["shadow_outcome_at"] = now
            elif -shadow_favourable >= 0.15 * shadow_atr:
                life["shadow_outcome"] = "MINUS_0_15_ATR_FIRST"
                life["shadow_outcome_at"] = now

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
            "entry_episode_no": payload.get("entry_episode_no"),
            "execution_window_state": payload.get("execution_window_state"),
            "route_health": payload.get("route_health"),
            "ret_5m_pct": payload.get("ret_5m_pct"),
            "relative_5m_vs_nifty_pct": payload.get("relative_5m_vs_nifty_pct"),
            "rvol_3m": payload.get("rvol_3m"),
            "rvol_3m_accel": payload.get("rvol_3m_accel"),
            "fresh_entry_gate": payload.get("fresh_entry_gate"),
            "fresh_entry_reason": payload.get("fresh_entry_reason"),
            "continuation_math": payload.get("continuation_math"),
            "risk_plan": payload.get("risk_plan"),
            "validation_label": "INTERIM / NOT VALIDATED",
        }
        try:
            _append_jsonl(self.event_file, record)
        except OSError:
            pass

    def _evaluate(self, now):
        self._maybe_reset_session(now)
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
            life = self._lifecycle.setdefault(symbol, {})
            locked_contract = life.get("locked_option_contract") or candidate.get("locked_option_contract")
            if setup.get("setup") and live_price:
                route = v122b_tactical.route_option(
                    option_snaps,
                    direction=setup.get("direction") or direction,
                    spot=live_price,
                    now=now,
                    speed_class=setup.get("speed_class") or "IMPULSE",
                    expected_underlying_move_abs=max(_f(setup.get("expected_move_abs"), 0.0), 0.25 * max(_f(candidate.get("atr"), 0.0), 0.0)),
                    earnings=event,
                    locked_contract_symbol=locked_contract,
                )

            five_minute = v122b_tactical.five_minute_witness(
                setup.get("direction") or direction,
                candidate.get("ret_5m_pct"),
                candidate.get("relative_5m_vs_nifty_pct"),
            )
            route_health = v122b_tactical.option_route_health(route)

            state = v122b_tactical.classify_state(
                setup,
                fast_veto=fast,
                stale=stale,
                depth_persist=persistence,
                option_route=route,
                active_same_direction=direction_used[setup.get("direction") or direction],
            )

            # Keep current execution honesty separate from opportunity
            # persistence. A bad quote is never called executable, but once an
            # entry episode has opened, a transient route-quality failure no
            # longer erases the underlying opportunity.
            if route_health.get("state") == "HEALTHY":
                life["last_executable_at"] = now
                if life.get("route_degraded_since") is not None:
                    life["route_recovered_at"] = now
                life["route_degraded_since"] = None
                life["route_blocked_since"] = None
            elif route_health.get("state") == "DEGRADED":
                if life.get("route_degraded_since") is None:
                    life["route_degraded_since"] = now
            elif route_health.get("state") == "BLOCKED":
                if life.get("route_blocked_since") is None:
                    life["route_blocked_since"] = now

            if (
                state.get("state") == "OPTION_NOT_TRADEABLE"
                and life.get("episode_open")
                and route_health.get("state") == "DEGRADED"
            ):
                state = {
                    "state": "ROUTE_DEGRADED",
                    "tradeable": False,
                    "reason": "entry window remains open; current option route degraded: " + str(route_health.get("reason") or "quote quality"),
                }

            fresh_episode_ok = True
            fresh_episode_reason = "OPEN_EPISODE_OR_FIRST_SIGNAL"
            if (
                not life.get("episode_open")
                and state.get("state") in ("READY", "TRIGGERED", "TRADEABLE")
                and life.get("last_closed_signature")
            ):
                fresh_episode_ok, fresh_episode_reason = self._fresh_episode_allowed(
                    life, setup, candidate, live_price, now
                )
                if not fresh_episode_ok:
                    state = {
                        "state": "READY",
                        "tradeable": False,
                        "reason": "prior episode ended; waiting for fresh structure/re-arm evidence",
                    }

            if state.get("state") == "TRADEABLE":
                direction_used[setup.get("direction") or direction] += 1

            # Start an entry episode when structure is READY or already
            # TRADEABLE and an executable option exists.  Contract selection
            # becomes sticky for this thesis; later re-routes are explicit.
            route_contract = (route or {}).get("contract") or {}
            if fresh_episode_ok and state.get("state") in ("READY", "TRIGGERED", "TRADEABLE") and (route or {}).get("tradeable") and route_contract.get("symbol"):
                if not life.get("episode_open"):
                    life["episode_no"] = int(life.get("episode_no") or 0) + 1
                    life["episode_open"] = True
                    life["episode_started_at"] = now
                    life["episode_ready_at"] = now if state.get("state") == "READY" else None
                    # Live episode fields describe the current episode only.
                    # Historical close metadata remains in transition/shadow logs.
                    life["episode_closed_at"] = None
                    life["episode_result"] = None
                    life["episode_exit_reason"] = None
                if life.get("locked_option_contract") != route_contract.get("symbol"):
                    life["locked_option_contract"] = route_contract.get("symbol")
                    life["locked_option_strike"] = route_contract.get("strike")
                    life["locked_option_delta"] = route_contract.get("delta")
                    life["locked_option_expiry"] = route_contract.get("expiry")
                    life["contract_locked_at"] = now
                    life["contract_selection_reason"] = (route or {}).get("selection_reason")
                    life["contract_reroute_reason"] = (route or {}).get("reroute_reason")

            state, life = self._manage_lifecycle(symbol, candidate, setup, state, now)

            # Freeze the option premium/delta reference at the actual underlying
            # trigger for consistent premium projections. READY-stage plans use
            # the live route reference and become frozen only after triggering.
            route_contract_now = (route or {}).get("contract") or {}
            if isinstance(life.get("triggered_at"), dt.datetime) and route_contract_now.get("symbol"):
                if life.get("plan_option_contract") != route_contract_now.get("symbol"):
                    life["plan_option_contract"] = route_contract_now.get("symbol")
                    life["plan_option_entry_mid"] = _f(route_contract_now.get("mid"))
                    life["plan_option_entry_delta"] = _f(route_contract_now.get("delta"))
                    life["plan_option_entry_gamma"] = max(0.0, _f(route_contract_now.get("gamma"), 0.0))

            if state.get("state") in ("EXIT", "TIME_EXIT") and life.get("episode_open"):
                life["episode_open"] = False
                life["episode_closed_at"] = now
                best = _f(life.get("best_favourable"), 0.0)
                expected = max(_f(setup.get("expected_move_abs"), 0.0), 1e-9)
                life["episode_result"] = (
                    "PROVEN_MOVE" if best >= v122b_tactical.PROPOSED_PROFIT_PROTECT_FRACTION * expected
                    else ("NO_FOLLOWTHROUGH" if state.get("state") == "TIME_EXIT" else "ENTRY_EXIT")
                )
                life["episode_exit_reason"] = state.get("reason")
                life["last_closed_signature"] = self._episode_signature(setup, candidate)
                life["last_closed_at"] = now
                life["last_closed_price"] = live_price
                life["last_closed_relative_5m"] = candidate.get("relative_5m_vs_nifty_pct")
                life["last_closed_ret_5m"] = candidate.get("ret_5m_pct")

            basis = self._basis(symbol, now)
            rvol3, rvol3_accel, rvol3_prev = self._rvol_3m(symbol, current, now)
            relative3 = self._relative_3m(symbol)
            option_route = route or {}
            risk = v122b_tactical.one_lot_risk_preview(
                option_route.get("contract"),
                spot=live_price,
                invalidation=setup.get("invalidation"),
            )
            risk_plan = v122b_tactical.dynamic_trade_plan(
                direction=setup.get("direction") or direction,
                spot=live_price,
                trigger=setup.get("trigger"),
                invalidation=setup.get("invalidation"),
                expected_move_abs=setup.get("expected_move_abs"),
                atr=candidate.get("atr"),
                best_favourable_abs=life.get("best_favourable"),
                worst_adverse_abs=life.get("worst_adverse"),
                completed_bars=bars,
                contract=option_route.get("contract"),
                entry_underlying=life.get("entry_underlying"),
                option_entry_mid=life.get("plan_option_entry_mid"),
                option_entry_delta=life.get("plan_option_entry_delta"),
                option_entry_gamma=life.get("plan_option_entry_gamma"),
                speed_class=setup.get("speed_class"),
            )
            route_degraded_seconds = None
            if isinstance(life.get("route_degraded_since"), dt.datetime):
                route_degraded_seconds = max(0.0, (now - life["route_degraded_since"]).total_seconds())

            execution_window_open = bool(
                life.get("episode_open")
                and state.get("state") not in ("EXIT", "TIME_EXIT", "CANCELLED", "STALE", "BLOCKED_EXPOSURE")
                and route_health.get("state") != "BLOCKED"
            )
            if not execution_window_open:
                execution_window_state = "CLOSED"
            elif state.get("state") == "PROFIT_PROTECT":
                execution_window_state = "MANAGE"
            elif state.get("state") == "ROUTE_DEGRADED":
                execution_window_state = "OPEN_WAIT_ROUTE"
            elif state.get("state") == "TRADEABLE":
                execution_window_state = "OPEN_ACTIVE"
            else:
                execution_window_state = "OPEN_READY"

            continuation_math = self._record_continuation_shadow(
                now=now, symbol=symbol, candidate=candidate, setup=setup, state=state,
                life=life, live_price=live_price, route_health=route_health, route=route,
                persistence=persistence, fast=fast, stale=stale, rvol3=rvol3,
                rvol3_accel=rvol3_accel, relative3=relative3, five_minute=five_minute,
                cash_age=cash_age, fut_age=fut_age,
                force_close=state.get("state") in ("EXIT", "TIME_EXIT"),
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
                "rvol_3m_accel": rvol3_accel,
                "rvol_3m_previous": rvol3_prev,
                "relative_3m_vs_nifty_pct": relative3,
                "ret_5m_pct": candidate.get("ret_5m_pct"),
                "relative_5m_vs_nifty_pct": candidate.get("relative_5m_vs_nifty_pct"),
                "five_minute_witness": five_minute,
                "route_health": route_health.get("state"),
                "route_health_reason": route_health.get("reason"),
                "route_degraded_seconds": round(route_degraded_seconds, 1) if route_degraded_seconds is not None else None,
                "last_executable_at": _iso(life.get("last_executable_at")) if isinstance(life.get("last_executable_at"), dt.datetime) else None,
                "execution_window_open": execution_window_open,
                "execution_window_state": execution_window_state,
                "underlying_triggered": bool(setup.get("triggered")),
                "fast_veto": fast,
                "earnings": event,
                "option_route": option_route,
                "one_lot_risk": risk,
                "risk_plan": risk_plan,
                "entry_episode_no": int(life.get("episode_no") or 0),
                "entry_episode_open": bool(life.get("episode_open")),
                "entry_episode_result": life.get("episode_result"),
                "entry_episode_exit_reason": life.get("episode_exit_reason"),
                "entry_episode_started_at": _iso(life.get("episode_started_at")) if isinstance(life.get("episode_started_at"), dt.datetime) else None,
                "entry_episode_closed_at": _iso(life.get("episode_closed_at")) if isinstance(life.get("episode_closed_at"), dt.datetime) else None,
                "locked_option_contract": life.get("locked_option_contract"),
                "locked_option_strike": life.get("locked_option_strike"),
                "locked_option_delta": life.get("locked_option_delta"),
                "locked_option_expiry": life.get("locked_option_expiry"),
                "contract_selection_reason": life.get("contract_selection_reason") or option_route.get("selection_reason"),
                "contract_reroute_reason": life.get("contract_reroute_reason") or option_route.get("reroute_reason"),
                "triggered_at": _iso(life.get("triggered_at")) if isinstance(life.get("triggered_at"), dt.datetime) else None,
                "best_favourable_abs": _f(life.get("best_favourable")),
                "trailing_invalidation": _f(life.get("trailing_invalidation")),
                "signal_age_seconds": round((now - life["triggered_at"]).total_seconds(), 1) if isinstance(life.get("triggered_at"), dt.datetime) else None,
                "fresh_entry_gate": bool(fresh_episode_ok),
                "fresh_entry_reason": fresh_episode_reason,
                "continuation_math": continuation_math,
            }
            old = self._last_states.get(symbol)
            self._log_transition(symbol, old, payload["state"], payload)
            self._last_states[symbol] = payload["state"]
            output.append(payload)

            # Preserve the just-closed payload for audit/history, then clear
            # the episode clock before the next evaluation.  This prevents a
            # later breakout (or next-day breakout) from inheriting the first
            # trade's timer and entry price.
            if state.get("state") in ("EXIT", "TIME_EXIT"):
                self._reset_trigger(life)

        priority = {
            "TRADEABLE": 10, "PROFIT_PROTECT": 9, "READY": 8, "TRIGGERED": 7,
            "ROUTE_DEGRADED": 7, "FORMING": 6, "OPTION_NOT_TRADEABLE": 5, "TIME_EXIT": 4,
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
            "continuation_shadow": {
                "status": "RECORDING_SHADOW",
                "samples_written": self._shadow_samples_written,
                "episodes_with_close_record": len(self._shadow_episode_keys),
                "file": Path(self.continuation_file).name if self.continuation_file else None,
                "controls_trading": False,
            },
            "rules": {
                "pool_max": v122b_tactical.TACTICAL_POOL_MAX,
                "pre_result_next_month_dte_lte": v122b_tactical.PROPOSED_PRE_RESULT_NEXT_MONTH_DTE,
                "max_friction_ratio": v122b_tactical.PROPOSED_MAX_FRICTION_TO_EXPECTED_MOVE,
                "same_direction_cap": v122b_tactical.PROPOSED_MAX_SAME_DIRECTION_ACTIVE,
                "risk_plan_target1_fraction": v122b_tactical.RISK_PLAN_TARGET1_FRACTION,
                "risk_plan_trailing_method": "PROVEN_3M_STRUCTURE_AFTER_T1",
                "risk_plan_atr3_shadow_length": v122b_tactical.RISK_PLAN_ATR3_LENGTH,
                "risk_plan_controls_trading": False,
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
                self._maybe_reset_session(now)
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
