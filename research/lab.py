"""Strategy lab: honest portfolio backtests on years of daily prices.

Rules from published research are tested with FIXED parameters chosen before
looking at results. Signals use the daily close; trades happen at the next
day's open; fees 0.1% + 0.05% slippage per side. The coin universe is rebuilt
every day from what was liquid at the time (delisted coins included), so the
test can't cheat by only picking today's survivors.

Usage: python research/lab.py path/to/research-data-dir
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

COST = 0.0015            # per side: 0.1% fee + 0.05% slippage
HOLDOUT = "2023-01-01"   # rules were designed on earlier data; this part is the real test
LOOKBACKS = [10, 20, 30, 60, 90, 150, 250, 360]


def load(d: Path):
    df = pd.read_csv(d / "daily.csv.gz", parse_dates=["date"])
    df = df.drop_duplicates(["symbol", "date"])
    wide = {k: df.pivot(index="date", columns="symbol", values=k).sort_index() for k in
            ["open", "high", "low", "close", "quote_volume"]}
    idx = pd.date_range(wide["close"].index.min(), wide["close"].index.max(), freq="D")
    wide = {k: v.reindex(idx) for k, v in wide.items()}
    return wide


# ---------- universe ----------

def universe(w, top: int = 20, min_age: int = 365, min_vol: float = 2e6):
    """Each day: the `top` most-traded coins (30-day median USDT volume) that
    have traded for at least `min_age` days. Uses only past data."""
    qv = w["quote_volume"]
    med = qv.rolling(30, min_periods=20).median()
    age = w["close"].notna().cumsum()
    ok = (age >= min_age) & (med >= min_vol) & w["close"].notna()
    rank = med.where(ok).rank(axis=1, ascending=False)
    return rank <= top


# ---------- signals: target weight per coin, decided at the close ----------

def donchian_ensemble(close: pd.DataFrame, lookbacks=LOOKBACKS) -> pd.DataFrame:
    """Fraction (0..1) of lookbacks currently long. Long when the close makes a
    new n-day high; exit when it falls below the trailing midpoint stop."""
    total = pd.DataFrame(0.0, index=close.index, columns=close.columns)
    for n in lookbacks:
        hi = close.rolling(n, min_periods=n).max()
        lo = close.rolling(n, min_periods=n).min()
        mid = (hi + lo) / 2
        c, h, m = close.values, hi.values, mid.values
        pos = np.zeros_like(c)
        stop = np.full(c.shape[1], np.nan)
        for t in range(1, len(c)):
            prev = pos[t - 1]
            entry = (prev == 0) & (c[t] >= h[t]) & ~np.isnan(h[t])
            stop = np.where(entry, m[t], np.where(prev == 1, np.fmax(stop, m[t]), np.nan))
            hold = (prev == 1) & (c[t] > stop)
            pos[t] = np.where(entry | hold, 1.0, 0.0)
            pos[t][np.isnan(c[t])] = 0
        total += pos
    return total / len(lookbacks)


def vol(close: pd.DataFrame, n: int = 90) -> pd.DataFrame:
    return close.pct_change(fill_method=None).rolling(n, min_periods=30).std() * np.sqrt(365)


def sma_gate(series: pd.Series, n: int = 200, band: float = 0.0) -> pd.Series:
    """1 while the close is above its n-day average (with a hysteresis band)."""
    s = series.rolling(n, min_periods=n).mean()
    out, on = [], 0.0
    for c, m in zip(series.values, s.values):
        if np.isnan(m):
            on = 0.0
        elif c > m * (1 + band):
            on = 1.0
        elif c < m * (1 - band):
            on = 0.0
        out.append(on)
    return pd.Series(out, index=series.index)


# ---------- simulator ----------

def simulate(w, target: pd.DataFrame, rebalance_band: float = 0.02) -> dict:
    """Trade toward target weights at the next open. Returns daily equity etc."""
    op, cl = w["open"], w["close"]
    target = target.reindex(columns=cl.columns).fillna(0.0)
    target = target.where(cl.notna(), 0.0)
    gross = target.sum(axis=1)
    target = target.div(gross.clip(lower=1.0), axis=0)        # never above 100% invested (spot)
    cols = list(cl.columns)
    O, C, T = op.values, cl.values, target.values
    n, k = O.shape
    hold = np.zeros(k)            # coin units held, in "equity units" at price
    cash, eq = 1.0, np.ones(n)
    turnover = 0.0
    trades = 0
    last_close = np.full(k, np.nan)
    for t in range(1, n):
        price_open = np.where(np.isnan(O[t]), last_close, O[t])
        # coins that vanished (delisted) are sold at the last known price
        val = hold * np.nan_to_num(price_open)
        equity = cash + val.sum()
        want = T[t - 1] * equity
        diff = want - val
        trade = np.abs(diff) > rebalance_band * equity
        trade &= ~np.isnan(price_open) | (hold == 0)
        trade |= (want == 0) & (val > 0)
        if trade.any():
            d = np.where(trade, diff, 0.0)
            fee = np.abs(d).sum() * COST
            cash -= d.sum() + fee
            with np.errstate(divide="ignore", invalid="ignore"):
                hold = hold + np.where(trade, np.nan_to_num(d / price_open), 0.0)
            turnover += np.abs(d).sum() / max(equity, 1e-9)
            trades += int(((hold > 0) & trade & (d > 0) & (val == 0)).sum())
        last_close = np.where(np.isnan(C[t]), last_close, C[t])
        eq[t] = cash + (hold * np.nan_to_num(last_close)).sum()
    equity = pd.Series(eq, index=cl.index)
    expo = target.sum(axis=1)
    return {"equity": equity, "exposure": expo, "turnover": turnover, "entries": trades}


def metrics(equity: pd.Series, start=None, end=None) -> dict:
    e = equity.loc[start:end].dropna()
    e = e[e.index >= e.index[0]]
    r = e.pct_change().dropna()
    if len(r) < 30:
        return {}
    years = len(r) / 365
    cagr = (e.iloc[-1] / e.iloc[0]) ** (1 / years) - 1
    sharpe = r.mean() / r.std() * np.sqrt(365) if r.std() > 0 else 0.0
    dd = (e / e.cummax() - 1).min()
    return {"cagr": round(cagr * 100, 1), "sharpe": round(sharpe, 2), "max_dd": round(dd * 100, 1),
            "total": round((e.iloc[-1] / e.iloc[0] - 1) * 100, 1)}


# ---------- strategies (fixed, published parameters) ----------

def strategies(w):
    cl = w["close"]
    btc = cl["BTC"]
    uni20 = universe(w, 20)
    v = vol(cl)
    out = {}

    out["Buy and hold BTC"] = pd.DataFrame({"BTC": (btc.notna()).astype(float)})
    gate = sma_gate(btc, 200, 0.02)
    out["BTC above 200-day average"] = pd.DataFrame({"BTC": gate})

    sig = donchian_ensemble(cl[["BTC", "ETH"]])
    vs = (0.25 / v[["BTC", "ETH"]]).clip(upper=1.0)
    out["Trend ensemble BTC+ETH (25% vol)"] = sig * vs / 2
    vs50 = (0.50 / v[["BTC", "ETH"]]).clip(upper=1.0)
    out["Trend ensemble BTC+ETH (50% vol)"] = sig * vs50 / 2

    names = [c for c in cl.columns if uni20[c].any()]
    sigU = donchian_ensemble(cl[names]).where(uni20[names], 0.0)
    for tv in (0.25, 0.50):
        raw = sigU * (tv / v[names]).clip(upper=1.0) / 20
        out[f"Trend ensemble top-20 ({int(tv*100)}% vol)"] = raw
        out[f"Trend ensemble top-20 ({int(tv*100)}% vol) + BTC gate"] = raw.mul(gate, axis=0)

    # weekly rotation: top 5 of the top 20 by 28-day return, only in a BTC uptrend
    ret28 = cl[names].pct_change(28, fill_method=None).where(uni20[names])
    rk = ret28.rank(axis=1, ascending=False)
    pick = (rk <= 5).astype(float)
    weekly = pick[pick.index.dayofweek == 0].reindex(pick.index).ffill().fillna(0.0)
    g2 = gate * sma_gate(btc, 50, 0.0)
    inv = (1 / v[names]).where(weekly > 0)
    wts = inv.div(inv.sum(axis=1), axis=0).fillna(0.0)
    out["Rotation top-5 of 20 + BTC gate"] = wts.mul(g2, axis=0)
    return out


def run(d: Path) -> dict:
    w = load(d)
    res = {}
    for name, target in strategies(w).items():
        sim = simulate(w, target)
        eq = sim["equity"]
        res[name] = {
            "design (2018-2022)": metrics(eq, "2018-01-01", "2022-12-31"),
            "holdout (2023-now)": metrics(eq, HOLDOUT),
            "last 365 days": metrics(eq, eq.index[-1] - pd.Timedelta(days=365)),
            "avg exposure %": round(float(sim["exposure"].loc[HOLDOUT:].mean() * 100), 1),
            "turnover/yr": round(sim["turnover"] / (len(eq) / 365), 1),
            "by_year": {str(y): metrics(eq, f"{y}-01-01", f"{y}-12-31").get("total") for y in range(2018, eq.index[-1].year + 1)},
            "_equity": eq,
        }
    return res


if __name__ == "__main__":
    res = run(Path(sys.argv[1]))
    for name, r in res.items():
        print(f"\n{name}")
        for k, v in r.items():
            if not k.startswith("_"):
                print(f"  {k}: {v}")
