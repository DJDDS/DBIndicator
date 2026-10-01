"""Pre-registered one-week evaluation of the v124 order-flow recorder.

Usage:  python research/orderflow_week_eval.py v124_orderflow_minutes.jsonl

Rules fixed BEFORE any data was seen (1 Oct 2026):
* Features at the close of minute t: l5_imb, tot_imb, delta_ratio, micro_bps,
  ofi_norm (added 1 Oct evening, still before any data existed).
* Entry at close of minute t+1 (one-minute skip), exit at close of t+5.
* Return is market-neutral: minus the mean of all recorded symbols that minute.
* PASS needs, after >= 5 sessions:
    1. mean per-minute rank IC >= 0.03, day-clustered t >= 2.0, same sign every day
       on at least 4 of 5 days;
    2. top-minus-bottom quintile forward return > median spread_bps (cost of
       crossing the cash spread once).
  Anything else is FAIL. No re-tuning of horizon or thresholds after the fact.
* Side readout (does not change the verdict): CALL side = mean RAW forward
  return of the top quintile, PUT side = mean RAW forward return of the bottom
  quintile; a side is usable only if its move exceeds the median spread.
* Exploratory signals (absorption, book-vs-trade divergence, flow acceleration,
  spread stress, volume burst) are printed but cannot pass this week; anything
  promising gets its own fresh test week.
"""
import json
import sys

import numpy as np
import pandas as pd

FEATURES = ["l5_imb", "tot_imb", "delta_ratio", "micro_bps", "ofi_norm"]
EXPLORATORY = ["absorption", "book_vs_trade", "flow_accel", "spread_stress", "vol_burst"]


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
        ret1 = 1e4 * close.pct_change().reindex(g.index)
        sigma = ret1.abs().rolling(30, min_periods=10).median()
        g["absorption"] = g["delta_ratio"] - ret1 / sigma.replace(0, np.nan)
        g["book_vs_trade"] = g["delta_ratio"] - g["l5_imb"]
        g["flow_accel"] = g["l5_imb"] - g["l5_imb"].shift(3)
        g["spread_stress"] = g["spread_bps"] / g["spread_bps"].rolling(30, min_periods=10).median()
        if "volume" in g:
            g["vol_burst"] = g["volume"] / g["volume"].rolling(30, min_periods=10).median()
        out.append(g.reset_index().rename(columns={"index": "minute"}))
    df = pd.concat(out)
    df["raw_fwd_bps"] = df["fwd_bps"]
    df["fwd_bps"] = df["fwd_bps"] - df.groupby("minute")["fwd_bps"].transform("mean")
    return df.dropna(subset=["fwd_bps"])


def evaluate(df):
    days = sorted(df["day"].unique())
    cost = float(df["spread_bps"].median())
    print(f"sessions={len(days)} rows={len(df)} symbols={df.symbol.nunique()} median_spread_bps={cost:.1f}")
    verdict = {}
    for f in FEATURES + EXPLORATORY:
        if f not in df:
            continue
        if f == EXPLORATORY[0]:
            print("--- exploratory (cannot pass this week) ---")
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
        call_side = sub.loc[q == 4, "raw_fwd_bps"].mean()
        put_side = sub.loc[q == 0, "raw_fwd_bps"].mean()
        if f in FEATURES:
            verdict[f] = ok
        tag = ("PASS" if ok else "FAIL") if f in FEATURES else "explore"
        print(f"{f:13s} IC={mean:+.3f} t={t:+.2f} sameSignDays={same_sign}/{len(by_day)} "
              f"Q5-Q1={spread:+.1f}bps CALL(top)={call_side:+.1f} PUT(bottom)={put_side:+.1f}  -> {tag}")
    print("OVERALL:", "PASS" if any(verdict.values()) else "FAIL")


if __name__ == "__main__":
    evaluate(add_target(load(sys.argv[1])))
