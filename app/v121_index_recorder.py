"""V12.1 NIFTY near-expiry index-volatility recorder.

Research-only. This module records executable NIFTY option-chain state and
supports a development-only volatility study. It does not create a trade signal
or unlock Trial 25.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import threading
import time
from pathlib import Path
from typing import Callable


def _date(value):
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str):
        try:
            return dt.date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _float(value):
    try:
        x = float(value)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def _token(row):
    try:
        return int(row.get("instrument_token"))
    except (TypeError, ValueError):
        return None


def _datetime(value):
    if isinstance(value, dt.datetime):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _iso_datetime(value):
    parsed = _datetime(value)
    return parsed.isoformat(timespec="seconds") if parsed is not None else None


IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
UTC = dt.timezone.utc


def _as_utc(value, *, naive_timezone):
    parsed = _datetime(value)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=naive_timezone)
    return parsed.astimezone(UTC)


def _age_seconds(observed_at, quoted_at):
    # scanner.now_ist() intentionally returns a naive IST wall clock, while
    # KiteTicker exchange_timestamp is delivered as a naive UTC wall clock in
    # production.  Attach the source clock explicitly, then subtract in UTC.
    observed = _as_utc(observed_at, naive_timezone=IST)
    quoted = _as_utc(quoted_at, naive_timezone=UTC)
    if observed is None or quoted is None:
        return None
    return round(max(0.0, (observed - quoted).total_seconds()), 3)


def _is_nifty(row):
    return str(row.get("name") or "").strip().upper() == "NIFTY"


def _find_index_ref(nse_rows, symbol):
    want = symbol.upper()
    for row in nse_rows or []:
        ts = str(row.get("tradingsymbol") or "").strip().upper()
        name = str(row.get("name") or "").strip().upper()
        if ts == want or name == want:
            tok = _token(row)
            if tok is not None:
                return dict(row)
    return None


def select_index_universe(nfo_rows, nse_rows, spot, today, strike_steps=12) -> dict:
    """Select nearest NIFTY expiry, ATM ladder and required reference tokens.

    Fails closed if NIFTY spot, INDIA VIX, a live NIFTY future or a paired
    nearest-expiry option ladder cannot be resolved.
    """
    spot = _float(spot)
    today = _date(today)
    if spot is None or spot <= 0 or today is None:
        return {"status": "WAITING_INSTRUMENTS", "tokens": [], "metadata": {}, "refs": {}}

    spot_ref = _find_index_ref(nse_rows, "NIFTY 50")
    vix_ref = _find_index_ref(nse_rows, "INDIA VIX")
    if not spot_ref or not vix_ref:
        return {"status": "WAITING_INSTRUMENTS", "tokens": [], "metadata": {}, "refs": {}}

    option_rows = [
        dict(r) for r in (nfo_rows or [])
        if _is_nifty(r) and str(r.get("instrument_type") or "").upper() in ("CE", "PE")
        and _date(r.get("expiry")) is not None and _date(r.get("expiry")) >= today
        and _float(r.get("strike")) is not None and _token(r) is not None
    ]
    expiries = sorted({_date(r.get("expiry")) for r in option_rows})
    if not expiries:
        return {"status": "WAITING_INSTRUMENTS", "tokens": [], "metadata": {}, "refs": {}}
    expiry = expiries[0]
    exp_rows = [r for r in option_rows if _date(r.get("expiry")) == expiry]
    strikes = sorted({_float(r.get("strike")) for r in exp_rows})
    if not strikes:
        return {"status": "WAITING_INSTRUMENTS", "tokens": [], "metadata": {}, "refs": {}}
    atm = min(strikes, key=lambda x: (abs(x - spot), x))
    atm_ix = strikes.index(atm)
    lo = max(0, atm_ix - max(0, int(strike_steps)))
    hi = min(len(strikes), atm_ix + max(0, int(strike_steps)) + 1)
    selected_strikes = set(strikes[lo:hi])
    chosen = [r for r in exp_rows if _float(r.get("strike")) in selected_strikes]

    # Require both CE and PE at each selected strike; incomplete ladders are
    # excluded rather than fabricated.
    by_strike = {}
    for row in chosen:
        by_strike.setdefault(_float(row.get("strike")), {})[str(row.get("instrument_type")).upper()] = row
    paired = []
    for strike in sorted(by_strike):
        pair = by_strike[strike]
        if "CE" in pair and "PE" in pair:
            paired.extend([pair["CE"], pair["PE"]])

    future_rows = [
        dict(r) for r in (nfo_rows or []) if _is_nifty(r)
        and str(r.get("instrument_type") or "").upper() == "FUT"
        and _date(r.get("expiry")) is not None and _date(r.get("expiry")) >= today
        and _token(r) is not None
    ]
    future_rows.sort(key=lambda r: _date(r.get("expiry")))
    if not paired or not future_rows:
        return {"status": "WAITING_INSTRUMENTS", "tokens": [], "metadata": {}, "refs": {}}
    future = future_rows[0]

    metadata = {}
    for row in paired:
        tok = _token(row)
        metadata[tok] = {
            "kind": "OPTION", "instrument_token": tok,
            "tradingsymbol": row.get("tradingsymbol"), "expiry": expiry.isoformat(),
            "strike": _float(row.get("strike")), "type": str(row.get("instrument_type")).upper(),
            "lot_size": row.get("lot_size"),
        }
    for kind, row in (("SPOT", spot_ref), ("VIX", vix_ref), ("FUTURE", future)):
        tok = _token(row)
        metadata[tok] = {
            "kind": kind, "instrument_token": tok,
            "tradingsymbol": row.get("tradingsymbol"),
            "expiry": _date(row.get("expiry")).isoformat() if _date(row.get("expiry")) else None,
            "strike": None, "type": None, "lot_size": row.get("lot_size"),
        }

    refs = {"spot_token": _token(spot_ref), "vix_token": _token(vix_ref), "future_token": _token(future)}
    return {
        "status": "READY", "expiry": expiry.isoformat(), "atm_strike": float(atm),
        "strikes": sorted({m["strike"] for m in metadata.values() if m["kind"] == "OPTION"}),
        "tokens": sorted(metadata), "metadata": metadata, "refs": refs,
    }


def _levels(rows):
    out = []
    for row in list(rows or [])[:5]:
        if not isinstance(row, dict):
            continue
        out.append({
            "price": _float(row.get("price")),
            "quantity": int(row.get("quantity") or 0),
            "orders": int(row.get("orders") or 0),
        })
    return out


def _base(meta, tick, refs, ts):
    return {
        "ts": ts.isoformat(timespec="seconds"),
        "kind": meta.get("kind"), "instrument_token": meta.get("instrument_token"),
        "tradingsymbol": meta.get("tradingsymbol"), "expiry": meta.get("expiry"),
        "strike": meta.get("strike"), "type": meta.get("type"), "lot_size": meta.get("lot_size"),
        "last_price": _float(tick.get("last_price")),
        "exchange_timestamp": _iso_datetime(tick.get("exchange_timestamp")),
        "last_trade_time": _iso_datetime(tick.get("last_trade_time")),
        "quote_age_seconds": _age_seconds(ts, tick.get("exchange_timestamp")),
        "volume": tick.get("volume_traded", tick.get("volume")), "oi": tick.get("oi"),
        "spot": _float(refs.get("spot")), "india_vix": _float(refs.get("vix")),
        "nifty_future": _float(refs.get("future")),
    }


def normalize_micro_tick(meta, tick, refs, ts) -> dict:
    out = _base(meta, tick or {}, refs or {}, ts)
    depth = (tick or {}).get("depth") or {}
    buys, sells = _levels(depth.get("buy")), _levels(depth.get("sell"))
    bid = buys[0]["price"] if buys and buys[0]["price"] and buys[0]["price"] > 0 else None
    ask = sells[0]["price"] if sells and sells[0]["price"] and sells[0]["price"] > 0 else None
    out.update({
        "best_bid": bid, "best_ask": ask,
        "bid_qty": buys[0]["quantity"] if buys else 0, "ask_qty": sells[0]["quantity"] if sells else 0,
        "spread": round(ask - bid, 10) if bid is not None and ask is not None and ask >= bid else None,
    })
    return out


def normalize_depth_tick(meta, tick, refs, ts) -> dict:
    out = normalize_micro_tick(meta, tick, refs, ts)
    depth = (tick or {}).get("depth") or {}
    out["depth"] = {"buy": _levels(depth.get("buy")), "sell": _levels(depth.get("sell"))}
    return out


def _read_state(path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _write_state(path, state: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, sort_keys=True, separators=(",", ":"), default=str), encoding="utf-8")
    tmp.replace(path)


def _append_jsonl(path, record) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True, separators=(",", ":"), default=str) + "\n")


def _elapsed(last_value, now: dt.datetime, seconds: int) -> bool:
    if not last_value:
        return True
    try:
        last = dt.datetime.fromisoformat(str(last_value))
        if last.tzinfo is None and now.tzinfo is not None:
            last = last.replace(tzinfo=now.tzinfo)
        return (now - last).total_seconds() >= seconds
    except (TypeError, ValueError):
        return True


def _state_age_seconds(now: dt.datetime, value):
    observed = _as_utc(now, naive_timezone=IST)
    prior = _as_utc(value, naive_timezone=IST)
    if observed is None or prior is None:
        return None
    return round(max(0.0, (observed - prior).total_seconds()), 3)


class IndexVolWriter:
    """Append two-tier NIFTY option research snapshots with atomic health state."""

    def __init__(self, root, state_file, *, micro_seconds=5, depth_seconds=60):
        self.root = Path(root)
        self.state_file = Path(state_file)
        self.micro_seconds = max(1, int(micro_seconds))
        self.depth_seconds = max(self.micro_seconds, int(depth_seconds))
        self._lock = threading.Lock()

    def set_universe(self, universe: dict) -> None:
        with self._lock:
            state = _read_state(self.state_file)
            state.update({
                "active_expiry": universe.get("expiry"),
                "atm_strike": _float(universe.get("atm_strike")),
                "token_count": len(universe.get("tokens") or []),
                "universe_updated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            })
            _write_state(self.state_file, state)

    def ingest(self, ticks: dict, metadata: dict, refs: dict, now: dt.datetime) -> dict:
        with self._lock:
            state = _read_state(self.state_file)
            day = now.date().isoformat()
            if state.get("current_day") != day:
                state.update({
                    "current_day": day, "micro_contract_rows": 0, "depth_contract_rows": 0,
                    "micro_snapshot_count": 0, "depth_snapshot_count": 0,
                    "last_micro_write_at": None, "last_depth_write_at": None,
                })
            micro_due = _elapsed(state.get("last_micro_write_at"), now, self.micro_seconds)
            depth_due = _elapsed(state.get("last_depth_write_at"), now, self.depth_seconds)
            option_items = [
                (tok, meta) for tok, meta in (metadata or {}).items()
                if meta.get("kind") == "OPTION" and tok in (ticks or {})
            ]
            micro_rows = [normalize_micro_tick(meta, ticks[tok], refs, now) for tok, meta in option_items] if micro_due else []
            depth_rows = [normalize_depth_tick(meta, ticks[tok], refs, now) for tok, meta in option_items] if depth_due else []
            try:
                if micro_due:
                    _append_jsonl(self.root / f"{day}_micro.jsonl", {"ts": now.isoformat(timespec="seconds"), "refs": refs, "rows": micro_rows})
                    state["last_micro_write_at"] = now.isoformat(timespec="seconds")
                    state["micro_contract_rows"] = int(state.get("micro_contract_rows") or 0) + len(micro_rows)
                    state["micro_snapshot_count"] = int(state.get("micro_snapshot_count") or 0) + 1
                if depth_due:
                    _append_jsonl(self.root / f"{day}_depth.jsonl", {"ts": now.isoformat(timespec="seconds"), "refs": refs, "rows": depth_rows})
                    state["last_depth_write_at"] = now.isoformat(timespec="seconds")
                    state["depth_contract_rows"] = int(state.get("depth_contract_rows") or 0) + len(depth_rows)
                    state["depth_snapshot_count"] = int(state.get("depth_snapshot_count") or 0) + 1
                state["last_tick_at"] = now.isoformat(timespec="seconds")
                state["session_last_tick_at"] = now.isoformat(timespec="seconds")
                state["last_write_error"] = None
                state["status"] = "RECORDING"
                _write_state(self.state_file, state)
            except OSError as exc:
                state["last_write_error"] = str(exc)
                state["status"] = "ERROR"
                try:
                    _write_state(self.state_file, state)
                except OSError:
                    pass
                return {"micro_written": False, "depth_written": False, "status": "ERROR", "error": str(exc)}
            return {"micro_written": micro_due, "depth_written": depth_due, "status": state.get("status")}


def _market_status(now: dt.datetime) -> str:
    if now.weekday() >= 5:
        return "MARKET_CLOSED"
    minute = now.hour * 60 + now.minute
    return "OPEN" if (9 * 60 + 15) <= minute <= (15 * 60 + 40) else "MARKET_CLOSED"


def index_recorder_health(root, state_file, *, now: dt.datetime, storage_mode: str, stale_after_seconds: int = 30) -> dict:
    root = Path(root)
    state = _read_state(state_file)
    day = now.date().isoformat()
    micro = root / f"{day}_micro.jsonl"
    depth = root / f"{day}_depth.jsonl"
    market = _market_status(now)
    tick_age = _state_age_seconds(now, state.get("last_tick_at"))
    if market == "MARKET_CLOSED":
        status = "MARKET_CLOSED"
    elif state.get("last_write_error"):
        status = "ERROR"
    elif tick_age is not None and tick_age <= max(1, int(stale_after_seconds)):
        status = "RECORDING"
    elif state.get("last_tick_at"):
        status = "STALE"
    else:
        status = state.get("status") or "WAITING"
    return {
        "recorder_status": status,
        "storage_mode": storage_mode,
        "storage_persistent": storage_mode == "PERSISTENT_VOLUME",
        "active_expiry": state.get("active_expiry"),
        "atm_strike": _float(state.get("atm_strike")),
        "token_count": int(state.get("token_count") or 0),
        "last_tick_at": state.get("last_tick_at"),
        "last_tick_age_seconds": tick_age,
        "last_micro_write_at": state.get("last_micro_write_at"),
        "last_depth_write_at": state.get("last_depth_write_at"),
        "micro_contract_rows": int(state.get("micro_contract_rows") or 0),
        "depth_contract_rows": int(state.get("depth_contract_rows") or 0),
        "micro_file_bytes": micro.stat().st_size if micro.exists() else 0,
        "depth_file_bytes": depth.stat().st_size if depth.exists() else 0,
        "connection_count": int(state.get("connection_count") or 0),
        "reconnect_count": int(state.get("reconnect_count") or 0),
        "last_error": state.get("last_error"),
        "last_write_error": state.get("last_write_error"),
    }


def _update_state(path, **fields):
    state = _read_state(path)
    state.update(fields)
    _write_state(path, state)
    return state


def _default_ticker_factory(api_key, access_token):
    from kiteconnect import KiteTicker
    return KiteTicker(api_key, access_token)


def _default_reactor_getter():
    try:
        from twisted.internet import reactor
        return reactor
    except Exception:
        return None


class IndexVolStreamService:
    """Fail-soft KiteTicker lifecycle for V12.1 NIFTY option recording."""

    def __init__(
        self, *, root, state_file, access_token_getter, kite_client_getter,
        ticker_factory=None, api_key="", now_provider=None, strike_steps=12,
        micro_seconds=5, depth_seconds=60, sleep_fn=time.sleep, reactor_getter=None,
        watchdog_stale_seconds=90,
    ):
        self.root = Path(root)
        self.state_file = Path(state_file)
        self.access_token_getter = access_token_getter
        self.kite_client_getter = kite_client_getter
        self.ticker_factory = ticker_factory or _default_ticker_factory
        self.api_key = api_key
        self.now_provider = now_provider or (lambda: dt.datetime.now().astimezone())
        self.strike_steps = max(1, int(strike_steps))
        self.writer = IndexVolWriter(self.root, self.state_file, micro_seconds=micro_seconds, depth_seconds=depth_seconds)
        self.sleep_fn = sleep_fn
        self.reactor_getter = reactor_getter or _default_reactor_getter
        self.watchdog_stale_seconds = max(15, int(watchdog_stale_seconds))
        self.latest_ticks = {}
        self.universe = None
        self._universe_day = None
        self._ticker = None
        self._ticker_active = False
        self._ticker_lock = threading.Lock()

    def _status(self, status, **extra):
        try:
            state = _update_state(self.state_file, status=status, **extra)
        except OSError:
            state = {"status": status, **extra}
        return state

    def _resolve(self, kite, now):
        nfo = kite.instruments("NFO") or []
        nse = kite.instruments("NSE") or []
        ltp = kite.ltp(["NSE:NIFTY 50"]) or {}
        spot = _float((ltp.get("NSE:NIFTY 50") or {}).get("last_price"))
        return select_index_universe(nfo, nse, spot, now.date(), strike_steps=self.strike_steps)

    def _refs(self):
        refs = (self.universe or {}).get("refs") or {}
        def lp(key):
            tick = self.latest_ticks.get(refs.get(key)) or {}
            return _float(tick.get("last_price"))
        return {"spot": lp("spot_token"), "vix": lp("vix_token"), "future": lp("future_token")}

    def _is_current_ticker(self, ticker):
        with self._ticker_lock:
            return self._ticker_active and self._ticker is ticker

    def _release_ticker(self, ticker):
        with self._ticker_lock:
            if self._ticker is ticker:
                self._ticker = None
                self._ticker_active = False

    def _dispatch_reactor(self, fn):
        reactor = self.reactor_getter()
        if reactor is not None and getattr(reactor, "running", False):
            reactor.callFromThread(fn)
            return True
        fn()
        return False

    def _close_ticker(self, ticker):
        def close_current():
            try:
                ticker.close()
            except Exception:
                pass
        self._dispatch_reactor(close_current)

    def _retire_ticker(self, ticker, *, status, error=None):
        self._release_ticker(ticker)
        state = self._status(status, connected=False, last_error=error)
        self._close_ticker(ticker)
        return state

    def _active_ticker_state(self, now):
        with self._ticker_lock:
            ticker = self._ticker if self._ticker_active else None
        if ticker is None:
            return None

        # A WebSocket from yesterday must never block today's instrument/expiry
        # rebuild. Retire it first; run_once will resolve and connect the new day
        # immediately in the same poll.
        if self._universe_day is not None and now.date() != self._universe_day:
            self._retire_ticker(ticker, status="STALE", error="previous trading-day websocket retired")
            return None

        state = _read_state(self.state_file) or {"status": "CONNECTING"}
        heartbeat = state.get("session_last_tick_at") or state.get("session_started_at")
        age = _state_age_seconds(now, heartbeat)
        if age is not None and age > self.watchdog_stale_seconds:
            return self._retire_ticker(
                ticker, status="STALE",
                error=f"no live websocket ticks for {int(age)} seconds",
            )
        return state

    def _connect_ticker(self, ticker):
        def connect_current():
            if not self._is_current_ticker(ticker):
                return
            try:
                ticker.connect(threaded=True)
            except Exception as exc:
                self._release_ticker(ticker)
                self._status("ERROR", connected=False, last_error=str(exc))
        self._dispatch_reactor(connect_current)

    def run_once(self, now=None) -> dict:
        now = now or self.now_provider()
        if _market_status(now) == "MARKET_CLOSED":
            with self._ticker_lock:
                ticker = self._ticker if self._ticker_active else None
            if ticker is not None:
                return self._retire_ticker(ticker, status="MARKET_CLOSED", error=None)
            return self._status("MARKET_CLOSED", connected=False)

        # threaded=True returns immediately.  run_forever therefore polls this
        # method while the WebSocket owns its own thread; never create a second
        # KiteTicker while that lifecycle is still active/reconnecting.
        active_state = self._active_ticker_state(now)
        if active_state is not None:
            return active_state

        token = self.access_token_getter()
        if not token:
            return self._status("WAITING_LOGIN")
        kite = self.kite_client_getter()
        if kite is None:
            return self._status("WAITING_LOGIN")
        try:
            universe = self._resolve(kite, now)
        except Exception as exc:  # REST/instrument failure is retryable, not fatal to scanner
            return self._status("WAITING_INSTRUMENTS", last_error=str(exc))
        if universe.get("status") != "READY":
            return self._status("WAITING_INSTRUMENTS", last_error="required NIFTY/VIX/option instruments unavailable")
        self.universe = universe
        self._universe_day = now.date()
        self.latest_ticks = {}
        self.writer.set_universe(universe)
        ticker = self.ticker_factory(self.api_key, token)
        lifecycle = {"rebuild_requested": False}

        # Claim the lifecycle before connect() starts its thread.  This closes
        # the small race where run_forever could poll again before on_connect.
        with self._ticker_lock:
            self._ticker = ticker
            self._ticker_active = True

        def on_connect(ws, response):
            if not self._is_current_ticker(ticker):
                return
            ws.subscribe(universe["tokens"])
            ws.set_mode(ws.MODE_FULL, universe["tokens"])
            prior = _read_state(self.state_file)
            _update_state(
                self.state_file, status="CONNECTED", connected=True,
                connection_count=int(prior.get("connection_count") or 0) + 1,
                last_connect_at=self.now_provider().isoformat(timespec="seconds"), last_error=None,
            )

        def on_ticks(ws, ticks):
            if not self._is_current_ticker(ticker):
                return
            tick_now = self.now_provider()
            for tick in ticks or []:
                tok = tick.get("instrument_token")
                if tok is not None:
                    self.latest_ticks[int(tok)] = dict(tick)
            refs = self._refs()
            self.writer.ingest(self.latest_ticks, universe["metadata"], refs, tick_now)
            # Rebuild on the next connection if the calendar day changed or spot
            # actually left the recorded strike ladder. This avoids needless
            # resubscriptions for ordinary ATM drift within the wide ladder.
            spot = refs.get("spot")
            strikes = universe.get("strikes") or []
            if tick_now.date() != self._universe_day or (spot is not None and strikes and (spot < min(strikes) or spot > max(strikes))):
                lifecycle["rebuild_requested"] = True
                try:
                    ws.close()
                except Exception:
                    self._release_ticker(ticker)

        def on_close(ws, code, reason):
            if not self._is_current_ticker(ticker):
                return
            # KiteTicker owns transient auto-reconnect.  Do not call connect()
            # from this callback and do not release the lifecycle on an abnormal
            # transient close; on_noreconnect is the terminal retry boundary.
            if lifecycle["rebuild_requested"] or code == 1000:
                self._release_ticker(ticker)
            _update_state(
                self.state_file, connected=False, status="DISCONNECTED",
                last_disconnect_at=self.now_provider().isoformat(timespec="seconds"),
                last_error=str(reason or "websocket closed"),
            )

        def on_error(ws, code, reason):
            if not self._is_current_ticker(ticker):
                return
            _update_state(self.state_file, status="ERROR", last_error=str(reason or code or "websocket error"))

        def on_reconnect(ws, attempts):
            if not self._is_current_ticker(ticker):
                return
            prior = _read_state(self.state_file)
            _update_state(
                self.state_file, status="RECONNECTING", connected=False,
                reconnect_count=int(prior.get("reconnect_count") or 0) + 1,
                reconnect_attempt=int(attempts or 0),
                last_error=None,
            )

        def on_noreconnect(ws):
            if not self._is_current_ticker(ticker):
                return
            self._release_ticker(ticker)
            _update_state(
                self.state_file, status="DISCONNECTED", connected=False,
                last_disconnect_at=self.now_provider().isoformat(timespec="seconds"),
                last_error="websocket reconnect attempts exhausted",
            )

        ticker.on_connect = on_connect
        ticker.on_ticks = on_ticks
        ticker.on_close = on_close
        ticker.on_error = on_error
        ticker.on_reconnect = on_reconnect
        ticker.on_noreconnect = on_noreconnect
        # The first lifecycle may start Twisted itself. Later trading sessions
        # reuse the still-running reactor, and Twisted APIs must then be invoked
        # on the reactor thread via callFromThread.
        self._status(
            "CONNECTING", connected=False, last_error=None,
            session_started_at=now.isoformat(timespec="seconds"), session_last_tick_at=None,
        )
        self._connect_ticker(ticker)
        return _read_state(self.state_file) or {"status": "WAITING"}

    def run_forever(self):
        backoff = 5
        while True:
            now = self.now_provider()
            try:
                state = self.run_once(now)
                status = state.get("status")
                if status in ("WAITING_LOGIN", "WAITING_INSTRUMENTS", "MARKET_CLOSED"):
                    self.sleep_fn(30 if status != "MARKET_CLOSED" else 60)
                else:
                    self.sleep_fn(backoff)
            except Exception as exc:  # absolute boundary: never die silently
                self._status("ERROR", last_error=str(exc))
                self.sleep_fn(backoff)
