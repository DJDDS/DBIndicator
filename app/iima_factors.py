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
    s = values.astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
    parsed = pd.Series(pd.NaT, index=s.index, dtype="datetime64[ns]")
    compact_mask = s.str.fullmatch(r"\d{6}", na=False)
    if compact_mask.any():
        parsed.loc[compact_mask] = pd.to_datetime(
            s.loc[compact_mask], format="%Y%m", errors="coerce"
        )
    other = ~compact_mask
    if other.any():
        parsed.loc[other] = pd.to_datetime(s.loc[other], errors="coerce")
    if parsed.isna().any():
        bad_pos = int(parsed.isna().to_numpy().nonzero()[0][0])
        raise ValueError(f"IIMA factor file contains unparseable month value: {s.iloc[bad_pos]!r}")
    return pd.DatetimeIndex(parsed).to_period("M").to_timestamp("M")


def parse_iima_monthly_factors(
    content: str | bytes,
    *,
    start: str | pd.Timestamp | None = None,
    end: str | pd.Timestamp | None = None,
    required_factors: tuple[str, ...] = _REQUIRED,
    require_complete_window: bool = False,
) -> pd.DataFrame:
    """Parse the pinned IIMA monthly factor CSV into decimal monthly returns.

    When ``start``/``end`` are supplied, only that preregistered consumer
    window is validated. Rows outside the requested window remain part of the
    pinned source hash but cannot block an experiment that never consumes them.
    ``required_factors`` lets FF3 consumers avoid requiring WML, which is not
    an input to the Trial-24 residualisation. ``require_complete_window`` makes
    a fully bounded monthly consumer fail closed if any month is absent.
    """
    if isinstance(content, bytes):
        content = content.decode("utf-8-sig", errors="replace")
    frame = pd.read_csv(io.StringIO(str(content)), keep_default_na=False)
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
    requested = tuple(str(x) for x in required_factors)
    unknown = [x for x in requested if x not in _REQUIRED]
    if unknown:
        raise ValueError(f"unknown IIMA factor requirement(s): {', '.join(unknown)}")
    missing = [x for x in ("date",) + requested if x not in chosen]
    if missing:
        raise ValueError(
            f"IIMA factor file missing required columns: {', '.join(missing)}; "
            f"received columns: {', '.join(str(c) for c in frame.columns)}"
        )

    months = _parse_month(frame[chosen["date"]])
    mask = pd.Series(True, index=frame.index)
    if start is not None:
        start_month = pd.Timestamp(start).to_period("M").to_timestamp("M")
        mask &= months >= start_month
    if end is not None:
        end_month = pd.Timestamp(end).to_period("M").to_timestamp("M")
        mask &= months <= end_month
    frame = frame.loc[mask].reset_index(drop=True)
    months = pd.DatetimeIndex(months[mask.to_numpy()])
    if frame.empty:
        raise ValueError("IIMA factor file has no rows in the requested window")

    if require_complete_window:
        if start is None or end is None:
            raise ValueError("complete IIMA factor window requires both start and end")
        expected_months = pd.period_range(start_month, end_month, freq="M").to_timestamp("M")
        missing_months = expected_months.difference(months)
        if len(missing_months):
            rendered = ", ".join(pd.Timestamp(x).strftime("%Y-%m") for x in missing_months)
            raise ValueError(
                f"IIMA factor file missing required month(s) in requested window: {rendered}"
            )

    out = pd.DataFrame(index=months)
    for col in requested:
        raw = frame[chosen[col]].astype(str)
        cleaned = (
            raw.str.replace("\u00a0", " ", regex=False)
            .str.strip()
            .str.replace("−", "-", regex=False)
            .str.replace("–", "-", regex=False)
            .str.replace("—", "-", regex=False)
            .str.replace(",", "", regex=False)
        )
        explicit_pct = cleaned.str.endswith("%")
        cleaned_numeric = cleaned.str.replace(r"%$", "", regex=True).str.strip()
        vals = pd.to_numeric(cleaned_numeric, errors="coerce")
        bad = vals.isna()
        if bad.any():
            pos = int(bad.to_numpy().nonzero()[0][0])
            month = pd.Timestamp(months[pos]).strftime("%Y-%m")
            raw_value = raw.iloc[pos]
            raise ValueError(
                f"IIMA factor column {col} contains non-numeric value at {month}: {raw_value!r}"
            )

        numeric = vals.to_numpy(dtype=float)
        pct_mask = explicit_pct.to_numpy(dtype=bool)
        converted = numeric.copy()
        converted[pct_mask] = converted[pct_mask] / 100.0

        plain = vals[~explicit_pct]
        if not plain.empty:
            # Official IIMA factor files are published in percentage points.
            # Preserve support for unmistakably-decimal synthetic fixtures while
            # treating ordinary published values as percentage points.
            plain_scale = 100.0 if float(plain.abs().quantile(0.90)) > 0.20 else 1.0
            converted[~pct_mask] = converted[~pct_mask] / plain_scale

        out[col] = converted
    out = out.sort_index()
    if out.index.has_duplicates:
        raise ValueError("IIMA factor file has duplicate months")
    return out[list(requested)]


class IIMAFactorClient:
    def __init__(self, cache_dir: str | Path, timeout: float = 20.0):
        self.cache_dir = Path(cache_dir)
        self.timeout = float(timeout)

    @property
    def cache_path(self) -> Path:
        return self.cache_dir / f"iima_four_factors_monthly_{PINNED_RELEASE}.csv"

    def load_monthly(
        self,
        *,
        start: str | pd.Timestamp | None = None,
        end: str | pd.Timestamp | None = None,
        required_factors: tuple[str, ...] = _REQUIRED,
        require_complete_window: bool = False,
    ) -> tuple[pd.DataFrame, dict]:
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
        frame = parse_iima_monthly_factors(
            raw,
            start=start,
            end=end,
            required_factors=required_factors,
            require_complete_window=require_complete_window,
        )
        return frame, {
            "release": PINNED_RELEASE,
            "url": PINNED_MONTHLY_URL,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "source": source,
            "rows": int(len(frame)),
        }
