"""Second round: the research-recommended altcoin rules (pre-registered, small
grid), including a weekly-retrained machine-learning ranker.
Usage: python research/alts2.py <research-data dir> <out.pkl>
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lab  # noqa: E402
from alts import alt_universe, summarise  # noqa: E402


def buffered_top(score: pd.DataFrame, uni: pd.DataFrame, k: int, keep: int, every: int = 7) -> pd.DataFrame:
    """Every `every` days pick the top k; a held coin stays while it ranks within `keep`."""
    s = score.where(uni)
    held: set = set()
    rows = []
    for i, (day, row) in enumerate(s.iterrows()):
        if i % every == 0:
            rk = row.dropna()
            rk = rk[rk > -np.inf].rank(ascending=False)
            held = {c for c in held if c in rk.index and rk[c] <= keep}
            for c in rk.sort_values().index:
                if len(held) >= k:
                    break
                held.add(c)
        rows.append({c: 1.0 / k for c in held})
    return pd.DataFrame(rows, index=s.index).reindex(columns=s.columns).fillna(0.0)


def ridge_ranker(w, uni, names, gate, k=5, lam=10.0, train_weeks=52):
    """Weekly: fit ridge regression of next-week return on cross-sectional
    feature ranks over the past 52 weeks (data available at the time only),
    then hold the k highest predicted coins."""
    cl, qv = w["close"][names], w["quote_volume"][names]
    feats = {}
    for n in (5, 10, 20, 50, 100, 200):
        feats[f"sma{n}"] = cl / cl.rolling(n, min_periods=n).mean()
    feats["hi28"] = cl / cl.rolling(28, min_periods=28).max()
    feats["r7"] = cl.pct_change(7, fill_method=None)
    feats["r28"] = cl.pct_change(28, fill_method=None)
    feats["vol30"] = cl.pct_change(fill_method=None).rolling(30).std()
    feats["lvol"] = np.log(qv.rolling(30).median() + 1)
    mondays = cl.index[cl.index.dayofweek == 0]
    u = uni[names]
    ranks = {f: (x.where(u).rank(axis=1, pct=True) - 0.5).loc[mondays] for f, x in feats.items()}
    fwd = cl.loc[mondays].pct_change(fill_method=None).shift(-1)          # next week's return
    fwd_r = fwd.where(u.loc[mondays]).rank(axis=1, pct=True) - 0.5
    X = np.stack([ranks[f].values for f in feats], axis=-1)            # weeks x coins x feats
    Y = fwd_r.values
    target = pd.DataFrame(0.0, index=mondays, columns=names)
    for t in range(train_weeks + 1, len(mondays)):
        xs, ys = X[t - train_weeks - 1:t - 1].reshape(-1, X.shape[-1]), Y[t - train_weeks - 1:t - 1].reshape(-1)
        ok = ~np.isnan(xs).any(axis=1) & ~np.isnan(ys)
        if ok.sum() < 200:
            continue
        a, b = xs[ok], ys[ok]
        beta = np.linalg.solve(a.T @ a + lam * np.eye(a.shape[1]), a.T @ b)
        xt = X[t]
        valid = ~np.isnan(xt).any(axis=1)
        pred = np.where(valid, np.nan_to_num(xt) @ beta, -np.inf)
        best = np.argsort(-pred)[:k]
        for j in best:
            if np.isfinite(pred[j]) and pred[j] > 0:
                target.iloc[t, j] = 1.0 / k
    return target.reindex(cl.index).ffill().fillna(0.0).mul(gate, axis=0)


def families(w, funding):
    cl = w["close"]
    btc = cl["BTC"]
    gate200 = lab.sma_gate(btc, 200, 0.02)
    gate100 = lab.sma_gate(btc, 100, 0.02)
    out = {}

    # 2. high-momentum ranking, top 50, ensemble of 7/28/90-day highs, buffer
    uni = alt_universe(w, 50)
    names = [c for c in cl.columns if uni[c].any()]
    c_, hi_ = cl[names], w["high"][names]
    score = sum(c_ / hi_.rolling(n, min_periods=n).max() for n in (7, 28, 90)) / 3
    above50 = c_ > c_.rolling(50, min_periods=50).mean()
    hm = buffered_top(score.where(above50), uni[names], 5, 10)
    out["F high-momentum top50 k5 buffer10 +BTC100"] = hm.mul(gate100, axis=0)
    out["F high-momentum top50 k5 buffer10 +BTC200"] = hm.mul(gate200, axis=0)

    # 3. relative strength vs BTC, top 30, every 2 weeks, BTC fallback
    uni30 = alt_universe(w, 30)
    n30 = [c for c in cl.columns if uni30[c].any()]
    ratio = cl[n30].div(btc, axis=0)
    rs = (ratio.pct_change(14, fill_method=None).rank(axis=1) + ratio.pct_change(28, fill_method=None).rank(axis=1)) / 2
    ok = (ratio > ratio.rolling(50, min_periods=50).mean()) & (cl[n30] > cl[n30].rolling(50, min_periods=50).mean())
    pick = buffered_top(rs.where(ok), uni30[n30], 5, 5, every=14)
    spare = (1 - pick.sum(axis=1)).clip(lower=0)
    rs_t = pick.copy()
    rs_t["BTC"] = spare * gate200
    out["G strength-vs-BTC top30 k5 2wk, BTC fallback"] = rs_t
    out["G strength-vs-BTC top30 k5 2wk, cash fallback"] = pick.mul(gate200, axis=0)

    # 4. machine-learning ranker (ridge), retrained weekly on the last 52 weeks
    for lam in (1.0, 100.0):
        out[f"H ridge ranker top50 k5 lam{lam:g} +BTC200"] = ridge_ranker(w, uni, names, gate200, lam=lam)

    # 5. funding overlay on the alt trend sleeve: halve when the crowd pays a lot to be long
    if funding is not None:
        from alts import families as fam1
        base = fam1(w)["A alt trend top20 cap1/10 +gate"]
        med = funding.reindex(columns=base.columns).where(base > 0).median(axis=1).reindex(base.index)
        hot = (med.rolling(7).mean() > 0.0009).astype(float)
        out["A alt trend top20 + funding overlay"] = base.mul(1 - 0.5 * hot, axis=0)
    return out


if __name__ == "__main__":
    d = Path(sys.argv[1])
    w = lab.load(d)
    f = pd.read_csv(d / "funding.csv.gz", parse_dates=["date"]).pivot(index="date", columns="symbol", values="funding")
    rets = {}
    for name, t in families(w, f).items():
        sim = lab.simulate(w, t)
        eq = sim["equity"]
        r = summarise(eq, sim)
        rets[name] = eq.pct_change()
        print(f"{name:46s} D sh {r['design'].get('sharpe')} dd {r['design'].get('max_dd')} cagr {r['design'].get('cagr')} | "
              f"H sh {r['holdout'].get('sharpe')} dd {r['holdout'].get('max_dd')} cagr {r['holdout'].get('cagr')} | "
              f"1y {r['last365'].get('total')} | expo {r['expo']}% turn {r['turn']}", flush=True)
    pd.DataFrame(rets).to_pickle(sys.argv[2])
