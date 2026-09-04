"""NSE corporate-action helpers for point-in-time monthly total returns.

Only actions that can be handled mechanically without economic assumptions are
applied.  Rights, mergers, demergers and unknown capital actions fail closed.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable


class UnhandledCorporateAction(RuntimeError):
    pass


def total_return_between(start_price: float, end_price: float, actions: Iterable[dict]) -> float:
    start = float(start_price)
    end = float(end_price)
    if start <= 0 or end < 0:
        raise ValueError("prices must be positive/non-negative")
    shares = 1.0
    cash = 0.0
    for action in sorted(list(actions or []), key=lambda x: str(x.get("ex_date") or "")):
        kind = str(action.get("kind") or "").upper().strip()
        if kind == "DIVIDEND":
            cash += shares * float(action.get("cash_per_share") or 0.0)
        elif kind in {"BONUS", "SPLIT"}:
            mult = float(action.get("share_multiplier") or 0.0)
            if mult <= 0:
                raise UnhandledCorporateAction(f"invalid {kind} multiplier")
            shares *= mult
        elif kind in {"", "NONE"}:
            continue
        else:
            raise UnhandledCorporateAction(f"unsupported corporate action: {kind}")
    return (shares * end + cash) / start - 1.0


def classify_nse_action(row: dict) -> dict:
    """Normalize a NSE corporate-action row; fail closed on ambiguous actions."""
    symbol = str(row.get("symbol") or row.get("sm_symbol") or "").strip().upper()
    subject = str(row.get("subject") or row.get("purpose") or row.get("desc") or "").strip()
    upper = subject.upper()
    ex_date = row.get("exDate") or row.get("ex_date") or row.get("exdate")
    base = {"symbol": symbol, "ex_date": str(ex_date or ""), "cash_per_share": 0.0, "share_multiplier": 1.0}

    if "DIVIDEND" in upper:
        # Prefer an explicit Rs/Re amount.  Percentage dividends need face value.
        m = re.search(r"(?:RS\.?|RE\.?|₹)\s*([0-9]+(?:\.[0-9]+)?)", subject, re.I)
        if m:
            cash = float(m.group(1))
        else:
            p = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*%", subject)
            face = row.get("faceVal") or row.get("face_value")
            if not p or face in (None, ""):
                raise UnhandledCorporateAction(f"cannot parse dividend: {subject}")
            cash = float(face) * float(p.group(1)) / 100.0
        return {**base, "kind": "DIVIDEND", "cash_per_share": cash}

    if "BONUS" in upper:
        m = re.search(r"(\d+)\s*[:/]\s*(\d+)", subject)
        if not m or int(m.group(2)) <= 0:
            raise UnhandledCorporateAction(f"cannot parse bonus: {subject}")
        new, old = int(m.group(1)), int(m.group(2))
        return {**base, "kind": "BONUS", "share_multiplier": (new + old) / old}

    if "SPLIT" in upper or "SUB-DIV" in upper or "SUB DIV" in upper:
        nums = [float(x) for x in re.findall(r"(?:RS\.?|RE\.?|₹)?\s*([0-9]+(?:\.[0-9]+)?)", subject, re.I)]
        if len(nums) < 2 or nums[-1] <= 0:
            raise UnhandledCorporateAction(f"cannot parse split: {subject}")
        old, new = nums[-2], nums[-1]
        return {**base, "kind": "SPLIT", "share_multiplier": old / new}

    risky = ("RIGHT", "DEMERGER", "MERGER", "AMALGAM", "SPIN", "SCHEME", "CAPITAL REDUCTION")
    if any(x in upper for x in risky):
        return {**base, "kind": "UNHANDLED"}
    return {**base, "kind": "NONE"}

# Official NSE corporate-actions endpoint used by V11.  The client caches raw
# JSON before normalization so provenance can hash the exact exchange response.
NSE_HOME = "https://www.nseindia.com"
NSE_CORPORATE_ACTIONS_API = "https://www.nseindia.com/api/corporates-corporateActions"


class NSECorporateActionClient:
    def __init__(self, *, session=None, cache_dir=None, timeout=20):
        import requests
        from pathlib import Path
        self.session = session or requests.Session()
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        self.timeout = float(timeout)
        if self.cache_dir is not None:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.session.headers.update({
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json,text/plain,*/*",
                "Referer": "https://www.nseindia.com/companies-listing/corporate-filings-actions",
            })
        except Exception:
            pass
        self._warmed = False

    def _warmup(self):
        if self._warmed:
            return
        try:
            self.session.get(NSE_HOME, timeout=self.timeout)
        finally:
            self._warmed = True

    def _cache_path(self, year):
        if self.cache_dir is None:
            return None
        return self.cache_dir / f"corporate_actions_{int(year)}.json"

    def fetch_year(self, year: int) -> tuple[list[dict], bytes]:
        import json
        from datetime import date
        y = int(year)
        path = self._cache_path(y)
        if path is not None and path.exists() and path.stat().st_size > 0:
            raw = path.read_bytes()
        else:
            self._warmup()
            params = {
                "index": "equities",
                "from_date": f"01-01-{y}",
                "to_date": f"31-12-{y}",
            }
            resp = self.session.get(NSE_CORPORATE_ACTIONS_API, params=params, timeout=self.timeout)
            if hasattr(resp, "raise_for_status"):
                resp.raise_for_status()
            raw = bytes(getattr(resp, "content", b"") or b"")
            if not raw:
                raise RuntimeError(f"empty NSE corporate-action response for {y}")
            if path is not None:
                tmp = path.with_suffix(path.suffix + ".tmp")
                tmp.write_bytes(raw)
                tmp.replace(path)
        payload = json.loads(raw.decode("utf-8-sig"))
        if isinstance(payload, dict):
            rows = payload.get("data") or payload.get("records") or []
        elif isinstance(payload, list):
            rows = payload
        else:
            rows = []
        if not isinstance(rows, list):
            raise ValueError(f"unexpected NSE corporate-action payload for {y}")
        return [dict(r) for r in rows if isinstance(r, dict)], raw

    def load_normalized(self, start_year: int, end_year: int, symbols=None) -> tuple[dict[str, list[dict]], dict]:
        import hashlib
        import pandas as pd
        wanted = {str(s).strip().upper() for s in symbols} if symbols is not None else None
        out: dict[str, list[dict]] = {}
        hashes = {}
        unparsed = []
        for year in range(int(start_year), int(end_year) + 1):
            rows, raw = self.fetch_year(year)
            hashes[str(year)] = hashlib.sha256(raw).hexdigest()
            for row in rows:
                symbol = str(row.get("symbol") or row.get("sm_symbol") or "").strip().upper()
                if not symbol or (wanted is not None and symbol not in wanted):
                    continue
                try:
                    action = classify_nse_action(row)
                    # Normalize ex-date to ISO so range filtering is deterministic.
                    dt = pd.to_datetime(action.get("ex_date"), errors="coerce", dayfirst=True)
                    if pd.isna(dt):
                        if action.get("kind") not in {"NONE", ""}:
                            raise UnhandledCorporateAction("unparseable ex-date")
                        continue
                    action["ex_date"] = pd.Timestamp(dt).date().isoformat()
                    if action.get("kind") != "NONE":
                        out.setdefault(symbol, []).append(action)
                except Exception as exc:
                    # Unknown action types must not silently disappear.  Record a
                    # synthetic UNHANDLED action so the affected symbol-month is NaN.
                    dt = pd.to_datetime(row.get("exDate") or row.get("ex_date"), errors="coerce", dayfirst=True)
                    if not pd.isna(dt):
                        out.setdefault(symbol, []).append({
                            "symbol": symbol,
                            "ex_date": pd.Timestamp(dt).date().isoformat(),
                            "kind": "UNHANDLED",
                            "cash_per_share": 0.0,
                            "share_multiplier": 1.0,
                            "reason": str(exc),
                        })
                    unparsed.append({"symbol": symbol, "year": year, "error": str(exc)})
        for acts in out.values():
            acts.sort(key=lambda x: x.get("ex_date") or "")
        return out, {"year_sha256": hashes, "unparsed_rows": unparsed}
