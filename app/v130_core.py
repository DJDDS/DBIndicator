"""V13.0 swing engine - pure computation core (no I/O, no Flask, no Kite).

Ported line-for-line from the research build (bear_build.py / bull_live2.py / gate study) so that the
live evening signal is identical to the researched one. Every input is a plain pandas object; the
loader in ``v130_swing`` fills them from Kite + NSE.  ``tests/test_v130_core.py`` checks the port.

Inputs (all indexed by trading date; wide frames have one column per symbol):
  O, H, L, C, VAL : adjusted daily open/high/low/close and traded value (rupees)
  DP              : delivery percentage (EQ series)
  FO              : dict of wide frames from the F&O end-of-day file:
                    fut_oi, ce_oi, pe_oi, fut_contracts, ce_contracts, pe_contracts, fut_close
  M               : Nifty 50 close (Series)
  VIX             : India VIX close (Series)
  FII             : DataFrame with columns fii_idx (index-futures long share), fii_stk (stock-futures long share)
  SECTOR_PX       : wide frame of sector index closes, columns = index names
  SECTOR_OF       : dict symbol -> sector index name
  BAN             : wide 0/1 frame (in F&O ban that day)
  RES             : wide 0/1 frame (results board meeting held that day)
  INS_S, INS_B    : wide frames, promoter market sells / buys (rupees) on each day
  UNIV            : wide bool frame (symbol was in the F&O list that day)
  TDTE, SINCE     : Series, trading days to / since the monthly expiry
"""
from __future__ import annotations

import math
from typing import Dict

import numpy as np
import pandas as pd

ST = ['r1', 'r3', 'r5', 'r10', 'r20', 'r60', 'r250', 'd200', 'd50', 'd20', 'dd52', 'du52', 'sig20', 'volratio_sig', 'vrank', 'clv', 'gap', 'hl', 'body',
      'downdays', 'updays', 'rsi2', 'pos20', 'hi20', 'at_h7', 'at_l7', 'at_l20', 'at_h20', 'valratio', 'val5ratio', 'delivratio', 'deliv', 'stock_beta', 'logval',
      'oi5', 'oi1', 'pcr', 'pcr5', 'optact', 'oi_rel', 'basis', 'ban_now', 'ban20', 'res_since', 'ins_sell', 'ins_buy', 'sec5', 'sec20', 'sec_d200', 'idio5', 'idio20', 'rel5', 'rel20']
GATE_VARS = ['breadth', 'm5', 'm20', 'mdd', 'nlow7', 'tdte', 'mrsi2', 'vix', 'vix_rel', 'vix_z', 'vix_pct', 'vix5', 'vix_peak']


def _lag(a: np.ndarray, k: int = 1) -> np.ndarray:
    return np.vstack([np.full((k, a.shape[1]), np.nan), a[:-k]])


def _roll(a: np.ndarray, n: int, f: str, mp: int | None = None) -> np.ndarray:
    return getattr(pd.DataFrame(a).rolling(n, min_periods=mp or n), f)().to_numpy(np.float64)


def stock_features(O, H, L, C, VAL, DP, FO: Dict[str, pd.DataFrame], M: pd.Series, BAN, RES, INS_S, INS_B, UNIV,
                   SECTOR_PX: pd.DataFrame | None = None, SECTOR_OF: Dict[str, str] | None = None, rows: str = 'last') -> pd.DataFrame:
    """Per-stock features. ``rows='last'`` returns the last date only (live); ``'all'`` returns every universe row (tests)."""
    days = C.index; syms = C.columns
    al = lambda F: F.reindex(index=days, columns=syms).to_numpy(np.float64)
    O_, H_, L_, C_, V_ = (al(x) for x in (O, H, L, C, VAL))
    T, S = C_.shape
    lC = np.log(C_); r1 = lC - _lag(lC)
    S200 = _roll(C_, 200, 'mean'); S50 = _roll(C_, 50, 'mean'); S20 = _roll(C_, 20, 'mean')
    CL7 = _roll(C_, 7, 'min'); CH7 = _roll(C_, 7, 'max'); CL20 = _roll(C_, 20, 'min'); CH20 = _roll(C_, 20, 'max')
    H20 = _roll(H_, 20, 'max'); L20 = _roll(L_, 20, 'min'); HI250 = _roll(C_, 250, 'max', 100); LO250 = _roll(C_, 250, 'min', 100)
    sig20 = _roll(r1, 20, 'std', 15); sig60 = _roll(r1, 60, 'std', 40)
    medval = _roll(V_, 20, 'median', 10); val5 = _roll(V_, 5, 'mean'); medval60 = _roll(V_, 60, 'median', 40)
    univ = UNIV.reindex(index=days, columns=syms).fillna(False).to_numpy(bool) & (C_ >= 20)
    vr = pd.DataFrame(np.where(univ, sig20, np.nan)).rank(axis=1, pct=True).to_numpy()
    Ms = M.reindex(days).astype(float); mr = np.log(Ms).diff(); mrs = pd.Series(mr.values)
    BETA = pd.DataFrame(r1).rolling(60, min_periods=40).cov(mrs).div(mrs.rolling(60, min_periods=40).var(), axis=0).clip(-.5, 3).to_numpy()
    down = np.zeros_like(C_); up = np.zeros_like(C_)
    for t in range(1, T):
        down[t] = np.where(r1[t] < 0, down[t - 1] + 1, 0); up[t] = np.where(r1[t] > 0, up[t - 1] + 1, 0)
    d_ = pd.DataFrame(C_).diff(); _u = d_.clip(lower=0).ewm(alpha=.5, adjust=False).mean(); _d = (-d_.clip(upper=0)).ewm(alpha=.5, adjust=False).mean()
    RSI2 = (100 - 100 / (1 + _u / _d)).to_numpy()
    DP_ = al(DP); DPm = _roll(DP_, 20, 'median', 10)
    FOI, CEO, PEO, FCT, CCT, PCT, FUTC = (al(FO[k]) for k in ('fut_oi', 'ce_oi', 'pe_oi', 'fut_contracts', 'ce_contracts', 'pe_contracts', 'fut_close'))
    with np.errstate(all='ignore'):
        oi5 = np.log(FOI / _lag(FOI, 5)); oi1 = np.log(FOI / _lag(FOI, 1)); pcr = np.log((PEO + 1) / (CEO + 1)); pcr5 = pcr - _lag(pcr, 5)
        optact = np.log((CCT + PCT + 1) / (FCT + 1)); oi_rel = np.log(FOI / _roll(FOI, 20, 'mean', 10)); basis = FUTC / C_ - 1
    BAN_ = BAN.reindex(index=days, columns=syms).fillna(0).to_numpy(np.float64); BAN20 = _roll(BAN_, 20, 'max', 1)
    RES_ = RES.reindex(index=days, columns=syms).fillna(0).to_numpy(np.float64)
    since_res = np.full((T, S), 999.0)
    for t in range(T): since_res[t] = np.where(RES_[t] > 0, 0, (since_res[t - 1] + 1) if t else 999)
    ins = lambda F: F.reindex(columns=syms).reindex(days.union(F.index)).fillna(0).rolling(10, min_periods=1).sum().reindex(days).to_numpy()
    INS_S_, INS_B_ = ins(INS_S), ins(INS_B)
    if rows == 'last':
        t_ = np.full(S, T - 1); s_ = np.arange(S); keep = univ[T - 1]
        t_, s_ = t_[keep], s_[keep]
    else:
        t_, s_ = np.where(univ); m = t_ >= 260; t_, s_ = t_[m], s_[m]
    g = lambda a: a[t_, s_]
    with np.errstate(all='ignore'):
        rng = g(H_) - g(L_)
        F = {'r1': g(r1), 'r3': g(lC) - g(_lag(lC, 3)), 'r5': g(lC) - g(_lag(lC, 5)), 'r10': g(lC) - g(_lag(lC, 10)), 'r20': g(lC) - g(_lag(lC, 20)),
             'r60': g(lC) - g(_lag(lC, 60)), 'r250': g(lC) - g(_lag(lC, 250)),
             'd200': g(C_) / g(S200) - 1, 'd50': g(C_) / g(S50) - 1, 'd20': g(C_) / g(S20) - 1, 'dd52': g(C_) / g(HI250) - 1, 'du52': g(C_) / g(LO250) - 1,
             'sig20': g(sig20), 'volratio_sig': g(sig20) / g(sig60), 'vrank': g(vr),
             'clv': np.where(rng > 0, (2 * g(C_) - g(H_) - g(L_)) / np.where(rng > 0, rng, 1), 0), 'gap': g(np.log(O_ / _lag(C_))), 'hl': rng / g(C_), 'body': g(np.log(C_ / O_)),
             'downdays': g(down), 'updays': g(up), 'rsi2': g(RSI2),
             'pos20': (g(C_) - g(L20)) / np.maximum(g(H20) - g(L20), 1e-9), 'hi20': g(C_) / g(H20) - 1,
             'at_h7': (g(C_) >= g(CH7)).astype(float), 'at_l7': (g(C_) <= g(CL7)).astype(float), 'at_l20': (g(C_) <= g(CL20)).astype(float), 'at_h20': (g(C_) >= g(CH20)).astype(float),
             'valratio': g(V_) / g(medval), 'val5ratio': g(val5) / g(medval60), 'delivratio': g(DP_) / g(DPm), 'deliv': g(DP_),
             'stock_beta': g(BETA), 'logval': np.log(g(medval) + 1),
             'oi5': g(oi5), 'oi1': g(oi1), 'pcr': g(pcr), 'pcr5': g(pcr5), 'optact': g(optact), 'oi_rel': g(oi_rel), 'basis': g(basis),
             'ban_now': g(BAN_), 'ban20': g(BAN20), 'res_since': g(since_res), 'ins_sell': np.log1p(g(INS_S_)), 'ins_buy': np.log1p(g(INS_B_))}
    X = pd.DataFrame({k: np.asarray(v, dtype=np.float64) for k, v in F.items()})
    X.insert(0, 'sym', syms[s_]); X.insert(0, 'date', days[t_])
    # market (needed for rel5/rel20)
    m5 = np.log(Ms / Ms.shift(5)); m20 = np.log(Ms / Ms.shift(20))
    X['m5'] = m5.reindex(X.date).values; X['m20'] = m20.reindex(X.date).values
    if SECTOR_PX is not None and SECTOR_OF:
        P = SECTOR_PX.reindex(days).ffill(limit=3); lp = np.log(P)
        SEC = {'sec5': lp - lp.shift(5), 'sec20': lp - lp.shift(20), 'sec_d200': P / P.rolling(200, min_periods=150).mean() - 1}
        for nm, Dm in SEC.items():
            X[nm] = [Dm.at[d, SECTOR_OF[s]] if SECTOR_OF.get(s) in Dm.columns else np.nan for d, s in zip(X.date, X.sym)]
    else:
        X['sec5'] = X['sec20'] = X['sec_d200'] = np.nan
    X['idio5'] = X.r5 - X.sec5; X['idio20'] = X.r20 - X.sec20
    X['rel5'] = X.r5 - X.stock_beta.fillna(1) * X.m5; X['rel20'] = X.r20 - X.stock_beta.fillna(1) * X.m20
    X['res_since'] = X.res_since.clip(upper=60)
    # extras used by the rules (not model inputs)
    L252 = _roll(L_, 252, 'min', 150)
    X['low52_band'] = g(C_) / g(L252)
    X['close'] = g(C_)
    return X


def market_features(C, UNIV, M: pd.Series, VIX: pd.Series, FII: pd.DataFrame | None, TDTE: pd.Series, SINCE: pd.Series | None = None) -> pd.DataFrame:
    """Evening market features (bear_build 'mk' block) + VIX regime features (vix_adapt) for every date."""
    days = C.index; C_ = C.to_numpy(np.float64)
    univ = UNIV.reindex(index=days, columns=C.columns).fillna(False).to_numpy(bool) & (C_ >= 20)
    S200 = _roll(C_, 200, 'mean'); CL7 = _roll(C_, 7, 'min'); CH7 = _roll(C_, 7, 'max')
    Ms = M.reindex(days).astype(float); mr = np.log(Ms).diff()
    mk = pd.DataFrame(index=days)
    mk['m1'] = mr; mk['m5'] = np.log(Ms / Ms.shift(5)); mk['m20'] = np.log(Ms / Ms.shift(20))
    _md = Ms.diff(); mk['mrsi2'] = 100 - 100 / (1 + _md.clip(lower=0).ewm(alpha=.5, adjust=False).mean() / (-_md.clip(upper=0)).ewm(alpha=.5, adjust=False).mean())
    mk['m200'] = Ms / Ms.rolling(200).mean() - 1; mk['mvol'] = mr.rolling(20).std() * np.sqrt(250); mk['mdd'] = Ms / Ms.rolling(250, min_periods=50).max() - 1
    mk['breadth'] = np.nansum((C_ > S200) & univ, 1) / np.maximum(np.nansum(univ & np.isfinite(S200), 1), 1)
    mk['nhigh7'] = np.nansum((C_ >= CH7) & univ, 1) / np.maximum(univ.sum(1), 1)
    mk['nlow7'] = np.nansum((C_ <= CL7) & univ, 1) / np.maximum(univ.sum(1), 1)
    vx = VIX.reindex(days).ffill(limit=3).astype(float)
    mk['vix'] = vx; mk['vix5'] = np.log(vx / vx.shift(5))
    v = VIX.dropna().astype(float).sort_index()
    V = pd.DataFrame({'vix_rel': v / v.rolling(250, min_periods=120).median(), 'vix_z': (v - v.rolling(60).mean()) / v.rolling(60).std(),
                      'vix_pct': v.rolling(250, min_periods=120).apply(lambda a: (a[:-1] < a[-1]).mean(), raw=True), 'vix_peak': v / v.rolling(20).max()})
    mk = mk.join(V.reindex(days))
    if FII is not None:
        f = FII.reindex(days); mk['fii_idx'] = f.fii_idx; mk['fii_idx5'] = f.fii_idx - f.fii_idx.shift(5); mk['fii_stk'] = f.fii_stk
    mk['tdte'] = TDTE.reindex(days)
    if SINCE is not None: mk['since'] = SINCE.reindex(days)
    mk['nifty_up200'] = Ms > Ms.rolling(200, min_periods=150).mean()
    mk['m10'] = np.log(Ms / Ms.shift(10))
    return mk


def gate_score(mk_row: pd.Series, gate_ref: dict) -> dict:
    """Committee gate: mean percentile of each PySR formula vs its full-history values. Open if >= gate_cut."""
    import functools
    pcts = []
    for f in gate_ref['formulas']:
        env = {'np': np, 'functools': functools, '__builtins__': {}}
        try:
            env.update({v: float(mk_row[v]) for v in f['vars']})
            with np.errstate(all='ignore'):
                val = float(eval(f['py'], env))  # noqa: S307 - frozen formulas shipped with the model, not user input
        except Exception:  # noqa: BLE001
            continue
        if not math.isfinite(val):
            continue
        arr = np.asarray(gate_ref['sorted_values'][str(f['id'])])
        pcts.append(np.searchsorted(arr, val, side='left') / len(arr))
    G = float(np.mean(pcts)) if pcts else float('nan')
    return {'G': G, 'n_formulas': len(pcts), 'cut': gate_ref['gate_cut'], 'open': bool(pcts) and G >= gate_ref['gate_cut']}
