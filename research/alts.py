"""Altcoin strategy families, tested with the same honest simulator as lab.py.

Each family has a handful of settings fixed BEFORE looking at results. Every
variant tried is counted, so the overfitting checks can account for it.
Usage: python research/alts.py <research-data dir>
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lab  # noqa: E402

MAJORS = {"BTC", "ETH"}


def alt_universe(w, top, min_age=365):
    u = lab.universe(w, top + 2, min_age=min_age)          # +2 so BTC/ETH don't eat slots
    for c in MAJORS & set(u.columns):
        u[c] = False
    return u


def weekly(df: pd.DataFrame) -> pd.DataFrame:
    """Rebalance on Mondays only: keep each Monday's choice for the week."""
    return df[df.index.dayofweek == 0].reindex(df.index).ffill().fillna(0.0)


def equal_top(score: pd.DataFrame, uni: pd.DataFrame, k: int, vol=None) -> pd.DataFrame:
    """Hold the k best-scoring coins (score must be > 0), equal or inverse-vol weight."""
    s = score.where(uni)
    rk = s.rank(axis=1, ascending=False)
    pick = ((rk <= k) & (s > 0)).astype(float)
    if vol is not None:
        inv = (1 / vol).where(pick > 0)
        wts = inv.div(inv.sum(axis=1), axis=0).fillna(0.0)
        # scale down when fewer than k coins qualify (rest stays in cash)
        wts = wts.mul(pick.sum(axis=1) / k, axis=0)
        return wts
    return pick / k


def families(w):
    cl, qv = w["close"], w["quote_volume"]
    btc = cl["BTC"]
    gate = lab.sma_gate(btc, 200, 0.02)
    v = lab.vol(cl)
    out = {}

    for top in (20, 50):
        uni = alt_universe(w, top)
        names = [c for c in cl.columns if uni[c].any()]
        u, c_, vv = uni[names], cl[names], v[names]

        # A. trend ensemble across alts, capital shared among active signals (cap 1/k each)
        sig = lab.donchian_ensemble(c_).where(u, 0.0)
        for k in (5, 10):
            raw = (sig * (0.5 / vv).clip(upper=1.0)).clip(upper=1.0) / k
            out[f"A alt trend top{top} cap1/{k} +gate"] = raw.mul(gate, axis=0)

        # B. distance to high: close / highest close of window (1 = at the high)
        for win in (90, 365):
            dist = c_ / c_.rolling(win, min_periods=win).max()
            mom = c_.pct_change(28, fill_method=None)
            score = (dist - 0.9).where(mom > 0)            # near the high and rising
            for k in (5, 10):
                out[f"B near-{win}d-high top{top} k{k} +gate"] = weekly(equal_top(score, u, k, vv)).mul(gate, axis=0)

        # C. strength vs BTC: alt/BTC ratio 90-day change, only alts in their own uptrend
        ratio = c_.div(btc, axis=0)
        rs = ratio.pct_change(90, fill_method=None)
        up = c_ > c_.rolling(50, min_periods=50).mean()
        for k in (5, 10):
            out[f"C beats-BTC 90d top{top} k{k} +gate"] = weekly(equal_top(rs.where(up), u, k, vv)).mul(gate, axis=0)

        # D. momentum with rising volume
        vol_up = qv[names].rolling(7).mean() > qv[names].rolling(60).mean()
        m28 = c_.pct_change(28, fill_method=None)
        for k in (5, 10):
            out[f"D mom28+volume top{top} k{k} +gate"] = weekly(equal_top(m28.where(vol_up), u, k, vv)).mul(gate, axis=0)

        # E. low volatility in uptrend
        lowv = (1 / vv).where(up)
        for k in (5, 10):
            out[f"E low-vol uptrend top{top} k{k} +gate"] = weekly(equal_top(lowv, u, k, vv)).mul(gate, axis=0)
    return out


def summarise(eq, sim):
    last = eq.index[-1] - pd.Timedelta(days=365)
    return {"design": lab.metrics(eq, "2018-01-01", "2022-12-31"), "holdout": lab.metrics(eq, lab.HOLDOUT),
            "last365": lab.metrics(eq, last),
            "expo": round(float(sim["exposure"].loc[lab.HOLDOUT:].mean() * 100)),
            "turn": round(sim["turnover"] / (len(eq) / 365), 1),
            "years": {str(y): lab.metrics(eq, f"{y}-01-01", f"{y}-12-31").get("total") for y in range(2018, eq.index[-1].year + 1)}}


if __name__ == "__main__":
    d = Path(sys.argv[1])
    w = lab.load(d)
    res, rets = {}, {}
    for name, t in families(w).items():
        sim = lab.simulate(w, t)
        eq = sim["equity"]
        res[name] = summarise(eq, sim)
        rets[name] = eq.pct_change()
        r = res[name]
        print(f"{name:42s} D sh {r['design'].get('sharpe')} dd {r['design'].get('max_dd')} cagr {r['design'].get('cagr')} | "
              f"H sh {r['holdout'].get('sharpe')} dd {r['holdout'].get('max_dd')} cagr {r['holdout'].get('cagr')} | "
              f"1y {r['last365'].get('total')} | expo {r['expo']}% turn {r['turn']}", flush=True)
    out = Path(sys.argv[2] if len(sys.argv) > 2 else "research/alts_results.json")
    out.write_text(json.dumps(res, indent=1, default=float))
    pd.DataFrame(rets).to_pickle(out.with_suffix(".returns.pkl"))
