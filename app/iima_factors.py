"""Pinned IIM Ahmedabad Indian Fama-French + momentum factor data helpers.

V11 uses a pinned, survivorship-bias-adjusted monthly release.  The parser is
kept separate from the network client so source bytes can be cached and hashed
before any alpha outcome is evaluated.
"""
from __future__ import annotations

import hashlib
import io
from pathlib import Path
import re

import pandas as pd
import requests

PINNED_RELEASE = "2025-12"
PINNED_MONTHLY_URL = (
    "https://faculty.iima.ac.in/iffm/Indian-Fama-French-Momentum/DATA/"
    f"{PINNED_RELEASE}_FourFactors_and_Market_Returns_Monthly_SurvivorshipBiasAdjusted.csv"
)
_REQUIRED = ("rm_rf", "smb", "hml", "wml", "rf")


def _norm_col(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).strip().lower())


def _parse_month(values: pd.Series) -> pd.DatetimeIndex:
    s = values.astype(str).str.strip()
    parsed = pd.to_datetime(s, errors="coerce")
    # pandas treats YYYYMM integers poorly in generic mode; recover explicitly.
    bad = parsed.isna()
    if bad.any():
        compact = s[bad].str.replace(r"\.0$", "", regex=True)
        recovered = pd.to_datetime(compact, format="%Y%m", errors="coerce")
        parsed.loc[bad] = recovered
    if parsed.isna().any():
        raise ValueError("IIMA factor file contains unparseable month values")
    return pd.DatetimeIndex(parsed).to_period("M").to_timestamp("M")


def parse_iima_monthly_factors(content: str | bytes) -> pd.DataFrame:
    """Parse the pinned IIMA monthly factor CSV into decimal monthly returns."""
    if isinstance(content, bytes):
        content = content.decode("utf-8-sig", errors="replace")
    frame = pd.read_csv(io.StringIO(str(content)))
    if frame.empty:
        raise ValueError("IIMA factor file is empty")

    aliases = {
        "date": {"date", "month", "yyyymm"},
        "rm_rf": {"rmrf", "mktrf", "mf", "marketpremium", "marketriskpremium", "marketminusrf"},
        "smb": {"smb"},
        "hml": {"hml"},
        "wml": {"wml", "mom", "momentum"},
        "rf": {"rf", "riskfree", "riskfreerate"},
    }
    normalized = {_norm_col(c): c for c in frame.columns}
    chosen: dict[str, str] = {}
    for target, names in aliases.items():
        for name in names:
            if name in normalized:
                chosen[target] = normalized[name]
                break
    missing = [x for x in ("date",) + _REQUIRED if x not in chosen]
    if missing:
        raise ValueError(
            f"IIMA factor file missing required columns: {', '.join(missing)}; "
            f"received columns: {', '.join(str(c) for c in frame.columns)}"
        )

    out = pd.DataFrame(index=_parse_month(frame[chosen["date"]]))
    for col in _REQUIRED:
        vals = pd.to_numeric(frame[chosen[col]], errors="coerce")
        if vals.isna().any():
            raise ValueError(f"IIMA factor column {col} contains non-numeric values")
        # Official factor files are published in percentage points.  Permit
        # already-decimal fixtures only when the scale is unmistakably small.
        scale = 100.0 if float(vals.abs().quantile(0.90)) > 0.20 else 1.0
        out[col] = vals.to_numpy(dtype=float) / scale
    out = out.sort_index()
    if out.index.has_duplicates:
        raise ValueError("IIMA factor file has duplicate months")
    return out[list(_REQUIRED)]


class IIMAFactorClient:
    def __init__(self, cache_dir: str | Path, timeout: float = 20.0):
        self.cache_dir = Path(cache_dir)
        self.timeout = float(timeout)

    @property
    def cache_path(self) -> Path:
        return self.cache_dir / f"iima_four_factors_monthly_{PINNED_RELEASE}.csv"

    def load_monthly(self) -> tuple[pd.DataFrame, dict]:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path = self.cache_path
        source = "cache"
        if path.exists():
            raw = path.read_bytes()
        else:
            resp = requests.get(PINNED_MONTHLY_URL, timeout=self.timeout)
            resp.raise_for_status()
            raw = resp.content
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_bytes(raw)
            tmp.replace(path)
            source = "network"
        frame = parse_iima_monthly_factors(raw)
        return frame, {
            "release": PINNED_RELEASE,
            "url": PINNED_MONTHLY_URL,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "source": source,
            "rows": int(len(frame)),
        }
