"""Index-only NSE index-option execution cost model for Stage 3 research.

This module is intentionally independent of Trial 25 and stock-option research.
It models the cash charges applied to one-lot NIFTY/BANKNIFTY/SENSEX option
round trips.  Rates are version-stamped so historical Stage-3 artifacts remain
reproducible if exchange or statutory rates change later.
"""
from __future__ import annotations

import math


FEE_MODEL_VERSION = "INDEX_NSE_EQ_OPT_2026_04_V1"
BROKERAGE_PER_ORDER = 20.0
STT_SELL_RATE = 0.0015
NSE_TXN_RATE = 0.0003553
SEBI_RATE = 10.0 / 10_000_000.0
STAMP_BUY_RATE = 0.00003
GST_RATE = 0.18


def calculate_option_charges(fills: list[dict]) -> dict:
    """Calculate the version-stamped NSE equity/index-option fee basket.

    Each fill must provide BUY/SELL, positive price and positive quantity.
    STT is applied to sell premium and rounded to the nearest rupee with
    paise >= 50 rounded up, matching contract-note treatment.
    """
    normalized: list[tuple[str, float]] = []
    for fill in fills or []:
        side = str(fill.get("side") or "").upper()
        price = float(fill.get("price") or 0.0)
        quantity = int(fill.get("quantity") or 0)
        if side not in ("BUY", "SELL") or price <= 0 or quantity <= 0:
            raise ValueError("fills require BUY/SELL, positive price and positive quantity")
        normalized.append((side, price * quantity))

    turnover = sum(value for _, value in normalized)
    sell_turnover = sum(value for side, value in normalized if side == "SELL")
    buy_turnover = sum(value for side, value in normalized if side == "BUY")
    brokerage = BROKERAGE_PER_ORDER * len(normalized)
    stt = float(math.floor(sell_turnover * STT_SELL_RATE + 0.5))
    exchange = turnover * NSE_TXN_RATE
    sebi = turnover * SEBI_RATE
    stamp = buy_turnover * STAMP_BUY_RATE
    gst = (brokerage + exchange + sebi) * GST_RATE
    total = brokerage + stt + exchange + sebi + stamp + gst

    return {
        "model_version": FEE_MODEL_VERSION,
        "turnover": round(turnover, 8),
        "brokerage": round(brokerage, 8),
        "stt": round(stt, 8),
        "exchange_transaction_charge": round(exchange, 8),
        "sebi_fee": round(sebi, 8),
        "stamp_duty": round(stamp, 8),
        "gst": round(gst, 8),
        "total": round(total, 8),
    }
