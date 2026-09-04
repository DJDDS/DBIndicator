"""Research provenance and clustered-inference helpers.

These helpers do not evaluate alpha. They make research inputs, event sets,
and clustered inference reproducible and auditable.
"""
from __future__ import annotations

import hashlib
import io
import json
import math
from pathlib import Path
from typing import Mapping

import numpy as np
import pandas as pd


def _jsonable_scalar(value):
    if value is None:
        return None
    if isinstance(value, (pd.Timestamp, np.datetime64)):
        return str(pd.Timestamp(value).isoformat())
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        v = float(value)
        return v if math.isfinite(v) else None
    if isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def stable_json_sha256(value) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=_jsonable_scalar, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def series_sha256(series: pd.Series) -> str:
    s = pd.Series(series).copy()
    frame = pd.DataFrame({"index": [str(x) for x in s.index], "value": [_jsonable_scalar(x) for x in s.tolist()]})
    return stable_json_sha256(frame.to_dict(orient="records"))


def dataframe_sha256(frame: pd.DataFrame) -> str:
    if frame is None or frame.empty:
        return stable_json_sha256([])
    x = frame.copy()
    x = x.sort_index()
    x = x.reindex(sorted(x.columns), axis=1)
    rows = []
    for idx, row in x.iterrows():
        item = {"__index__": str(idx)}
        for c in x.columns:
            item[str(c)] = _jsonable_scalar(row[c])
        rows.append(item)
    return stable_json_sha256(rows)


def validate_sector_panel(*, research_symbols, sector_map: Mapping[str, str], sector_history_by_symbol: Mapping[str, pd.DataFrame]) -> dict:
    expected = sorted({str(sector_map.get(s)) for s in research_symbols if sector_map.get(s)})
    loaded = sorted(
        sec for sec in expected
        if isinstance((sector_history_by_symbol or {}).get(sec), pd.DataFrame)
        and not (sector_history_by_symbol or {}).get(sec).empty
    )
    missing = sorted(set(expected) - set(loaded))
    return {
        "complete": not missing,
        "expected": expected,
        "loaded": loaded,
        "missing": missing,
        "expected_count": len(expected),
        "loaded_count": len(loaded),
    }


def build_v10_input_manifest(*, research_symbols, sector_map, sector_history_by_symbol, histories,
                             gate_battery_version: str, cost_model: Mapping, market_history=None, cash_by_symbol=None) -> dict:
    symbols = sorted({str(s).upper() for s in research_symbols})
    sector_status = validate_sector_panel(
        research_symbols=symbols,
        sector_map=sector_map,
        sector_history_by_symbol=sector_history_by_symbol,
    )
    membership = {}
    lots = {}
    basis_inputs = {}
    cash_hashes = {}
    for sym in symbols:
        payload = (histories or {}).get(sym) or {}
        if isinstance(payload.get("membership"), pd.Series):
            membership[sym] = series_sha256(payload["membership"])
        if isinstance(payload.get("lot_size"), pd.Series):
            lots[sym] = series_sha256(payload["lot_size"])
        basis_piece = {}
        for key in ("near_settle", "next_settle", "near_expiry", "next_expiry"):
            if isinstance(payload.get(key), pd.Series):
                basis_piece[key] = series_sha256(payload[key])
        if basis_piece:
            basis_inputs[sym] = stable_json_sha256(basis_piece)
        cash_frame = (cash_by_symbol or {}).get(sym) if isinstance(cash_by_symbol, Mapping) else None
        if isinstance(cash_frame, pd.DataFrame) and not cash_frame.empty:
            cash_hashes[sym] = dataframe_sha256(cash_frame)
    sector_hashes = {
        sec: dataframe_sha256((sector_history_by_symbol or {}).get(sec))
        for sec in sector_status["expected"]
    }
    manifest = {
        "research_symbols": symbols,
        "universe_sha256": stable_json_sha256(symbols),
        "sector_map_sha256": stable_json_sha256({s: sector_map.get(s) for s in symbols}),
        "sector_histories_expected": sector_status["expected_count"],
        "sector_histories_loaded": sector_status["loaded_count"],
        "missing_sector_histories": sector_status["missing"],
        "sector_panel_complete": sector_status["complete"],
        "sector_history_sha256_by_name": sector_hashes,
        "membership_sha256_by_symbol": membership,
        "lot_size_sha256_by_symbol": lots,
        "basis_inputs_sha256_by_symbol": basis_inputs,
        "cash_history_sha256_by_symbol": cash_hashes,
        "market_history_sha256": dataframe_sha256(market_history) if isinstance(market_history, pd.DataFrame) and not market_history.empty else None,
        "gate_battery_version": str(gate_battery_version),
        "cost_model": dict(cost_model or {}),
        "cost_model_sha256": stable_json_sha256(dict(cost_model or {})),
    }
    manifest["manifest_sha256"] = stable_json_sha256(manifest)
    return manifest


def clustered_mean_inference(values, clusters) -> dict:
    frame = pd.DataFrame({"value": pd.to_numeric(values, errors="coerce"), "cluster": pd.Series(clusters).astype(str)})
    frame = frame.dropna(subset=["value"]).copy()
    n = int(len(frame))
    g = int(frame["cluster"].nunique()) if n else 0
    if n < 2:
        return {
            "mean": float(frame["value"].mean()) if n else None,
            "naive_se": None, "naive_t": None, "cluster_se": None, "event_cluster_t": None,
            "design_effect": None, "effective_n": n, "unequal_cluster_size": None,
            "rho": None, "rho_status": "NOT_IDENTIFIED_INSUFFICIENT_CLUSTERS",
        }
    vals = frame["value"].to_numpy(dtype=float)
    mean = float(np.mean(vals))
    sd = float(np.std(vals, ddof=1))
    naive_se = sd / math.sqrt(n) if sd > 0 else None
    naive_t = mean / naive_se if naive_se and naive_se > 0 else None
    if g < 2:
        return {
            "mean": mean, "naive_se": naive_se, "naive_t": naive_t,
            "cluster_se": None, "event_cluster_t": None, "design_effect": None,
            "effective_n": n, "unequal_cluster_size": float(n), "rho": None,
            "rho_status": "NOT_IDENTIFIED_INSUFFICIENT_CLUSTERS",
        }
    centered = frame.assign(centered=frame["value"] - mean).groupby("cluster")["centered"].sum().to_numpy(dtype=float)
    cluster_var = (g / (g - 1.0)) * float(np.sum(centered ** 2)) / float(n ** 2)
    cluster_se = math.sqrt(max(cluster_var, 0.0))
    cluster_t = mean / cluster_se if cluster_se > 0 else None
    counts = frame.groupby("cluster").size().to_numpy(dtype=float)
    m_eff = float(np.sum(counts ** 2) / np.sum(counts)) if np.sum(counts) > 0 else None
    if naive_se and naive_se > 0:
        de = float((cluster_se / naive_se) ** 2)
        effective_n = float(min(n, n / de)) if de > 0 else float(n)
    else:
        de = None
        effective_n = float(n)
    rho = None
    rho_status = "NOT_IDENTIFIED"
    if de is not None and m_eff is not None and m_eff > 1.0:
        candidate = float((de - 1.0) / (m_eff - 1.0))
        lower = -1.0 / max(m_eff - 1.0, 1.0)
        if math.isfinite(candidate) and lower <= candidate < 0.95:
            rho = candidate
            rho_status = "IDENTIFIED"
        elif math.isfinite(candidate) and candidate >= 0.95:
            rho_status = "NOT_IDENTIFIED_RHO_AT_OR_ABOVE_0_95"
        else:
            rho_status = "NOT_IDENTIFIED_OUT_OF_BOUNDS"
    return {
        "mean": mean,
        "naive_se": naive_se,
        "naive_t": naive_t,
        "cluster_se": cluster_se,
        "event_cluster_t": cluster_t,
        "design_effect": de,
        "effective_n": effective_n,
        "unequal_cluster_size": m_eff,
        "rho": rho,
        "rho_status": rho_status,
        "cluster_count": g,
        "event_count": n,
    }


def _canonical_event_csv_bytes(events: pd.DataFrame) -> bytes:
    x = events.copy()
    cols = sorted(str(c) for c in x.columns)
    x = x.reindex(columns=cols)
    sort_cols = [c for c in ("date", "symbol", "sector") if c in x.columns]
    if sort_cols:
        x = x.sort_values(sort_cols, kind="mergesort")
    else:
        x = x.sort_index(kind="mergesort")
    if "date" in x.columns:
        x["date"] = pd.to_datetime(x["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    return x.to_csv(index=False, lineterminator="\n", float_format="%.12g").encode("utf-8")


def persist_event_artifact(path, events: pd.DataFrame) -> dict:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _canonical_event_csv_bytes(events)
    digest = hashlib.sha256(payload).hexdigest()
    # Compression metadata can vary; the content hash is over canonical CSV,
    # while the persisted artifact is compact gzip.
    canonical = pd.read_csv(io.BytesIO(payload))
    canonical.to_csv(path, index=False, compression="gzip", lineterminator="\n", float_format="%.12g")
    return {"path": str(path), "row_count": int(len(events)), "content_sha256": digest}
