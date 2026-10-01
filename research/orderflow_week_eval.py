"""Pre-registered one-week evaluation of the v124 order-flow recorder.

Usage:  python research/orderflow_week_eval.py v124_orderflow_minutes.jsonl

Rules fixed BEFORE any data was seen (1 Oct 2026):
* Features at the close of minute t: l5_imb, tot_imb, delta_ratio, micro_bps.
* Entry at close of minute t+1 (one-minute skip), exit at close of t+5.
* Return is market-neutral: minus the mean of all recorded symbols that minute.
* PASS needs, after >= 5 sessions:
    1. mean per-minute rank IC >= 0.03, day-clustered t >= 2.0, same sign every day
       on at least 4 of 5 days;
    2. top-minus-bottom quintile forward return > median spread_bps (cost of
       crossing the cash spread once).
  Anything else is FAIL. No re-tuning of horizon or thresholds after the fact.
"""
import json
import sys

import numpy as np
import pandas as pd

FEATURES = ["l5_imb", "tot_imb", "delta_ratio", "micro_bps"]


def load(path):
    rows = [json.loads(x) for x in open(path, encoding="utf-8") if x.strip()]
    df = pd.DataFrame(rows)
    df["minute"] = pd.to_datetime(df["minute"])
    df["day"] = df["minute"].dt.date
    return df.sort_values(["symbol", "minute"])


def add_target(df):
    out = []
    for _, g in df.groupby(["symbol", "day"]):
        g = g.set_index("minute").sort_index()
        full = g.reindex(pd.date_range(g.index.min(), g.index.max(), freq="1min"))
        close = full["close"]
        fwd = 1e4 * (close.shift(-5) / close.shift(-1) - 1)
        g["fwd_bps"] = fwd.reindex(g.index)
        out.append(g.reset_index().rename(columns={"index": "minute"}))
    df = pd.concat(out)
    df["fwd_bps"] = df["fwd_bps"] - df.groupby("minute")["fwd_bps"].transform("mean")
    return df.dropna(subset=["fwd_bps"])


def evaluate(df):
    days = sorted(df["day"].unique())
    cost = float(df["spread_bps"].median())
    print(f"sessions={len(days)} rows={len(df)} symbols={df.symbol.nunique()} median_spread_bps={cost:.1f}")
    verdict = {}
    for f in FEATURES:
        sub = df.dropna(subset=[f])
        ics = (
            sub.groupby("minute")
            .filter(lambda g: len(g) >= 5)
            .groupby("minute")
            .apply(lambda g: g[f].rank().corr(g["fwd_bps"].rank()))
        )
        by_day = ics.groupby(ics.index.date).mean()
        mean = by_day.mean()
        t = mean / (by_day.std(ddof=1) / np.sqrt(len(by_day))) if len(by_day) > 1 else np.nan
        same_sign = int((np.sign(by_day) == np.sign(mean)).sum())
        q = sub.groupby("minute")[f].transform(lambda s: pd.qcut(s.rank(method="first"), 5, labels=False) if len(s) >= 5 else np.nan)
        spread = sub.loc[q == 4, "fwd_bps"].mean() - sub.loc[q == 0, "fwd_bps"].mean()
        ok = (
            len(by_day) >= 5 and abs(mean) >= 0.03 and abs(t) >= 2.0
            and same_sign >= len(by_day) - 1 and abs(spread) > cost
        )
        verdict[f] = ok
        print(f"{f:12s} IC={mean:+.3f} t={t:+.2f} sameSignDays={same_sign}/{len(by_day)} "
              f"Q5-Q1={spread:+.1f}bps  -> {'PASS' if ok else 'FAIL'}")
    print("OVERALL:", "PASS" if any(verdict.values()) else "FAIL")


if __name__ == "__main__":
    evaluate(add_target(load(sys.argv[1])))
