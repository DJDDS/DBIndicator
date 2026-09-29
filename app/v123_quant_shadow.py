"""Research-only live shadow recorder for the V12.3 quant regime model.

This recorder is deliberately non-controlling:
- it consumes an internal all-symbol observer payload,
- writes causal underlying-only features and latent-state transitions,
- never changes Focus Desk, tactical states, alerts, options, or execution.

The worker is asynchronous and bounded so model fitting / research I/O cannot
block the live scanner callback.
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import logging
import math
import os
import queue
import threading
from collections import defaultdict
from pathlib import Path

import numpy as np

from .v123_quant_regime import (
    GaussianRegimeHMM,
    QuantFeatureState,
    model_snapshot,
    posterior_lifecycle_step,
)

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
DEFAULT_SAMPLE_SECONDS = 60
DEFAULT_KEEP_SESSIONS = 10
DEFAULT_MIN_TRAINING_SESSIONS = 2
MIN_SEQUENCE_OBSERVATIONS = 20


def _iso(now):
    return now.isoformat(timespec="seconds")


def _parse_date_from_name(path: Path):
    name = path.name
    prefix = "features_"
    suffix = ".jsonl.gz"
    if not (name.startswith(prefix) and name.endswith(suffix)):
        return None
    try:
        return dt.date.fromisoformat(name[len(prefix):-len(suffix)])
    except ValueError:
        return None


def _atomic_json(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, separators=(",", ":"), default=str)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _session_open(now):
    if now.weekday() >= 5:
        return False
    minute = now.hour * 60 + now.minute
    return 9 * 60 + 15 <= minute <= 15 * 60 + 30


class QuantRegimeShadowRecorder:
    def __init__(
        self,
        root,
        *,
        sample_seconds=DEFAULT_SAMPLE_SECONDS,
        keep_sessions=DEFAULT_KEEP_SESSIONS,
        min_training_sessions=DEFAULT_MIN_TRAINING_SESSIONS,
    ):
        self.root = Path(root)
        self.sample_seconds = int(sample_seconds)
        self.keep_sessions = int(keep_sessions)
        self.min_training_sessions = int(min_training_sessions)
        self._lock = threading.Lock()
        self._trade_date = None
        self._last_sample_at = None
        self._last_nifty_price = None
        self._last_sector_price = {}
        self._sector_factor_status = "UNAVAILABLE"
        self._sector_factor_coverage = 0.0
        self._features = {}
        self._posterior = {}
        self._lifecycle = {}
        self._last_lifecycle = {}
        self._model = None
        self._model_status = "COLLECTING_BASELINE"
        self._training_sessions = []
        self._last_error = None
        self._latest = {}
        self._state_path = self.root / "shadow_state.json"

    def _feature_path(self, day):
        return self.root / f"features_{day.isoformat()}.jsonl.gz"

    def _event_path(self, day):
        return self.root / f"regimes_{day.isoformat()}.jsonl.gz"

    def _model_path(self, day):
        return self.root / f"model_for_{day.isoformat()}.json"

    def _feature_files_before(self, day):
        rows = []
        if self.root.exists():
            for path in self.root.glob("features_*.jsonl.gz"):
                pday = _parse_date_from_name(path)
                if pday is not None and pday < day:
                    rows.append((pday, path))
        rows.sort()
        return rows[-self.keep_sessions:]

    def _load_sequences(self, files):
        sequences = []
        for _, path in files:
            by_symbol = defaultdict(list)
            try:
                with gzip.open(path, "rt", encoding="utf-8") as fh:
                    for line in fh:
                        try:
                            row = json.loads(line)
                            x = row.get("x")
                            symbol = str(row.get("symbol") or "")
                            if symbol and isinstance(x, list) and len(x) == 4:
                                arr = np.asarray(x, dtype=float)
                                if np.all(np.isfinite(arr)):
                                    by_symbol[symbol].append(arr)
                        except Exception:
                            continue
            except OSError:
                continue
            for rows in by_symbol.values():
                if len(rows) >= MIN_SEQUENCE_OBSERVATIONS:
                    sequences.append(np.vstack(rows))
        return sequences

    def _fit_prior_model(self, day):
        files = self._feature_files_before(day)
        self._training_sessions = [d.isoformat() for d, _ in files]
        if len(files) < self.min_training_sessions:
            self._model = None
            self._model_status = "COLLECTING_BASELINE"
            return

        model_file = self._model_path(day)
        if model_file.exists():
            try:
                snap = json.loads(model_file.read_text(encoding="utf-8"))
                model = GaussianRegimeHMM()
                model.means = np.asarray(snap["means"], dtype=float)
                model.variances = np.asarray(snap["variances"], dtype=float)
                model.transition = np.asarray(snap["transition"], dtype=float)
                model.initial = np.asarray(snap["initial"], dtype=float)
                model._require_fit()
                self._model = model
                self._model_status = "ACTIVE_SHADOW"
                return
            except Exception:
                log.exception("Failed to restore quant-regime shadow model")

        sequences = self._load_sequences(files)
        if not sequences:
            self._model = None
            self._model_status = "COLLECTING_BASELINE"
            return
        model = GaussianRegimeHMM(max_iter=20, tol=1e-4)
        model.fit_sequences(sequences)
        snap = model_snapshot(model)
        snap.update({
            "fit_for_trade_date": day.isoformat(),
            "training_sessions": list(self._training_sessions),
            "sequence_count": len(sequences),
            "production_controls": False,
        })
        _atomic_json(model_file, snap)
        self._model = model
        self._model_status = "ACTIVE_SHADOW"

    def _restore_today(self, day):
        path = self._feature_path(day)
        if not path.exists():
            return
        last_ts = None
        try:
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                for line in fh:
                    row = json.loads(line)
                    symbol = str(row.get("symbol") or "")
                    price = row.get("price")
                    if not symbol or price is None:
                        continue
                    sector = str(row.get("sector") or row.get("sector_index") or "")
                    sector_price = row.get("sector_price")
                    if sector and sector_price is not None:
                        try:
                            sector_price = float(sector_price)
                        except (TypeError, ValueError):
                            sector_price = None
                        if sector_price is not None and math.isfinite(sector_price) and sector_price > 0:
                            self._last_sector_price[sector] = sector_price
                    q = self._features.setdefault(symbol, QuantFeatureState())
                    x = q.update(
                        price=price,
                        market_return=row.get("market_return"),
                        sector_return=row.get("sector_return"),
                        participation=row.get("participation"),
                    )
                    if self._model is not None:
                        filtered, posterior = self._model.filter_step(
                            x, posterior=self._posterior.get(symbol)
                        )
                        self._posterior[symbol] = posterior
                        life = posterior_lifecycle_step(
                            self._lifecycle.get(symbol), filtered["posterior"]
                        )
                        self._lifecycle[symbol] = life
                    ts = row.get("ts")
                    if ts and (last_ts is None or ts > last_ts):
                        last_ts = ts
        except Exception:
            log.exception("Failed to replay current quant-regime shadow features")
        if last_ts:
            try:
                self._last_sample_at = dt.datetime.fromisoformat(last_ts)
            except ValueError:
                pass

    def _roll_session(self, day):
        self.root.mkdir(parents=True, exist_ok=True)
        self._trade_date = day
        self._last_sample_at = None
        self._last_nifty_price = None
        self._last_sector_price = {}
        self._sector_factor_status = "UNAVAILABLE"
        self._sector_factor_coverage = 0.0
        self._features = {}
        self._posterior = {}
        self._lifecycle = {}
        self._last_lifecycle = {}
        self._latest = {}
        self._last_error = None
        self._fit_prior_model(day)
        self._restore_today(day)
        self._rotate()

    def _rotate(self):
        files = []
        if self.root.exists():
            for path in self.root.glob("features_*.jsonl.gz"):
                pday = _parse_date_from_name(path)
                if pday is not None:
                    files.append((pday, path))
        files.sort()
        keep_days = {d for d, _ in files[-self.keep_sessions:]}
        for day, path in files:
            if day in keep_days:
                continue
            for candidate in (
                path,
                self._event_path(day),
                self._model_path(day),
            ):
                try:
                    candidate.unlink(missing_ok=True)
                except OSError:
                    pass

    def _append_gzip_rows(self, path, rows):
        if not rows:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "at", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, separators=(",", ":"), default=str) + "\n")

    def process(self, payload, *, now):
        with self._lock:
            try:
                if not _session_open(now):
                    return self.status(now=now)
                if self._trade_date != now.date():
                    self._roll_session(now.date())
                if self._last_sample_at is not None:
                    if (now - self._last_sample_at).total_seconds() < self.sample_seconds:
                        return self.status(now=now)

                quant_rows = list((payload or {}).get("quant_rows") or [])
                nifty_price = ((payload or {}).get("nifty") or {}).get("live_price")
                if not quant_rows or nifty_price is None:
                    return self.status(now=now)

                try:
                    nifty_price = float(nifty_price)
                except (TypeError, ValueError):
                    return self.status(now=now)
                if not math.isfinite(nifty_price) or nifty_price <= 0:
                    return self.status(now=now)

                if self._last_nifty_price and self._last_nifty_price > 0:
                    market_return = math.log(nifty_price / self._last_nifty_price)
                else:
                    market_return = 0.0
                self._last_nifty_price = nifty_price

                # Compute one sector return per sector per sample.  Do this
                # before the stock loop so all stocks in the same sector see
                # exactly the same contemporaneous factor return.
                sector_contexts = dict((payload or {}).get("sector_contexts") or {})
                sector_returns = {}
                sector_prices = {}
                for sector, ctx in sector_contexts.items():
                    try:
                        sector_price = float((ctx or {}).get("live_price"))
                    except (TypeError, ValueError):
                        continue
                    if not math.isfinite(sector_price) or sector_price <= 0:
                        continue
                    sector = str(sector)
                    prior_sector_price = self._last_sector_price.get(sector)
                    if prior_sector_price and prior_sector_price > 0:
                        sector_returns[sector] = math.log(sector_price / prior_sector_price)
                    else:
                        sector_returns[sector] = 0.0
                    sector_prices[sector] = sector_price
                self._last_sector_price.update(sector_prices)

                feature_rows = []
                regime_rows = []
                latest = {}
                for row in quant_rows:
                    symbol = str(row.get("symbol") or "")
                    price = row.get("live_price")
                    if not symbol or price is None:
                        continue
                    try:
                        price = float(price)
                    except (TypeError, ValueError):
                        continue
                    if not math.isfinite(price) or price <= 0:
                        continue
                    participation = row.get("volume_rate_accel")
                    sector = str(row.get("sector_index") or row.get("sector") or "")
                    sector_return = sector_returns.get(sector) if sector else None
                    sector_price = sector_prices.get(sector) if sector else None
                    q = self._features.setdefault(symbol, QuantFeatureState())
                    x = q.update(
                        price=price,
                        market_return=market_return,
                        sector_return=sector_return,
                        participation=participation,
                    )
                    base = {
                        "schema_version": SCHEMA_VERSION,
                        "ts": _iso(now),
                        "trade_date": now.date().isoformat(),
                        "symbol": symbol,
                        "price": price,
                        "market_return": market_return,
                        "sector": sector or None,
                        "sector_price": sector_price,
                        "sector_return": sector_return,
                        "sector_status": "ACTIVE" if sector_return is not None else "UNAVAILABLE",
                        "participation": participation,
                        "x": [float(v) for v in x],
                        "production_controls": False,
                    }
                    feature_rows.append(base)

                    if self._model is None:
                        latest[symbol] = {
                            "symbol": symbol,
                            "status": "COLLECTING_BASELINE",
                            "lifecycle": None,
                            "latent_state": None,
                        }
                        continue

                    filtered, posterior = self._model.filter_step(
                        x, posterior=self._posterior.get(symbol)
                    )
                    self._posterior[symbol] = posterior
                    prior_life = self._lifecycle.get(symbol)
                    life = posterior_lifecycle_step(prior_life, filtered["posterior"])
                    self._lifecycle[symbol] = life
                    state_row = {
                        **base,
                        "latent_state": filtered["latent_state"],
                        "posterior": filtered["posterior"],
                        "lifecycle": life,
                        "lifecycle_changed": life != prior_life,
                    }
                    latest[symbol] = {
                        "symbol": symbol,
                        "price": price,
                        "latent_state": filtered["latent_state"],
                        "posterior": filtered["posterior"],
                        "lifecycle": life,
                    }
                    if life != self._last_lifecycle.get(symbol):
                        regime_rows.append(state_row)
                        self._last_lifecycle[symbol] = life

                with_sector = sum(1 for row in feature_rows if row.get("sector_return") is not None)
                if feature_rows:
                    self._sector_factor_coverage = with_sector / len(feature_rows)
                    if with_sector == len(feature_rows):
                        self._sector_factor_status = "ACTIVE"
                    elif with_sector:
                        self._sector_factor_status = "PARTIAL"
                    else:
                        self._sector_factor_status = "UNAVAILABLE"

                self._append_gzip_rows(self._feature_path(now.date()), feature_rows)
                self._append_gzip_rows(self._event_path(now.date()), regime_rows)
                self._last_sample_at = now
                self._latest = latest
                self._last_error = None
                self._persist_status(now)
                return self.status(now=now)
            except Exception as exc:
                self._last_error = str(exc)
                log.exception("Quant regime shadow recorder failed")
                self._persist_status(now)
                return self.status(now=now)

    def _persist_status(self, now):
        _atomic_json(self._state_path, self.status(now=now))

    def status(self, *, now):
        counts = defaultdict(int)
        for row in self._latest.values():
            life = row.get("lifecycle")
            if life:
                counts[life] += 1
        actionable = [
            row for row in self._latest.values()
            if str(row.get("lifecycle") or "").startswith("ACTIONABLE_")
        ]
        return {
            "schema_version": SCHEMA_VERSION,
            "status": self._model_status,
            "research_only": True,
            "production_controls": False,
            "trade_date": self._trade_date.isoformat() if self._trade_date else None,
            "last_sample_at": _iso(self._last_sample_at) if self._last_sample_at else None,
            "sample_seconds": self.sample_seconds,
            "training_sessions": list(self._training_sessions),
            "tracked_symbols": len(self._latest),
            "lifecycle_counts": dict(counts),
            "actionable_count": len(actionable),
            "actionable": actionable[:30],
            "sector_factor_status": self._sector_factor_status,
            "sector_factor_coverage": round(self._sector_factor_coverage, 4),
            "last_error": self._last_error,
            "asof": _iso(now),
        }


class QuantRegimeShadowWorker:
    """Bounded latest-snapshot worker; cannot block the market-stream callback."""

    def __init__(self, recorder):
        self.recorder = recorder
        self._queue = queue.Queue(maxsize=1)
        self._thread = None
        self._lock = threading.Lock()
        self._latest_status = {
            "status": "NOT_STARTED",
            "research_only": True,
            "production_controls": False,
        }

    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._run,
                name="v123-quant-regime-shadow",
                daemon=True,
            )
            self._thread.start()

    def submit(self, payload, now):
        self.start()
        item = (dict(payload or {}), now)
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(item)
            except queue.Full:
                pass

    def _run(self):
        while True:
            payload, now = self._queue.get()
            try:
                self._latest_status = self.recorder.process(payload, now=now)
            except Exception:
                log.exception("Quant regime shadow worker failed")

    def status(self):
        return dict(self._latest_status)


def list_shadow_artifacts(root):
    """Return a read-only manifest of quant-shadow research artifacts."""
    base = Path(root)
    out = []
    if not base.exists():
        return out
    for path in sorted(base.iterdir()):
        if not path.is_file():
            continue
        name = path.name
        kind = None
        if name.startswith("features_") and name.endswith(".jsonl.gz"):
            kind = "features"
        elif name.startswith("regimes_") and name.endswith(".jsonl.gz"):
            kind = "regimes"
        elif name.startswith("model_for_") and name.endswith(".json"):
            kind = "model"
        elif name == "shadow_state.json":
            kind = "status"
        if kind is None:
            continue
        out.append({
            "name": name,
            "kind": kind,
            "size_bytes": path.stat().st_size,
        })
    return out


def resolve_shadow_artifact(root, name):
    """Resolve only a manifest-listed artifact; traversal and secrets are excluded."""
    candidate_name = str(name or "")
    if "/" in candidate_name or "\\" in candidate_name or candidate_name in {"", ".", ".."}:
        return None
    allowed = {row["name"] for row in list_shadow_artifacts(root)}
    if candidate_name not in allowed:
        return None
    base = Path(root).resolve()
    path = (base / candidate_name).resolve()
    try:
        path.relative_to(base)
    except ValueError:
        return None
    return path if path.is_file() else None
