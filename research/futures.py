"""Futures version of the trend signals: shorts, leverage and funding costs.

Perpetual futures differ from spot in three ways that change results:
  * shorts are possible (sell when the trend is down),
  * leverage multiplies both gains and drops, and a big enough drop
    liquidates the position (modelled per coin: isolated margin),
  * holders pay or receive funding every 8h (longs usually pay).
Fees: 0.06% taker + 0.04% slippage per side. Trades at the next day's open.
Usage: python research/futures.py <research-data dir>
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lab  # noqa: E402

COST = 0.0010
COINS = ["BTC", "ETH"]


def short_votes(close: pd.DataFrame, lookbacks=lab.LOOKBACKS) -> pd.DataFrame:
    """Mirror of the long rule: a vote goes short on a new n-day low and covers
    when the close rises above the falling midpoint stop."""
    return lab.donchian_ensemble(1 / close, lookbacks)


def simulate(w, target: pd.DataFrame, funding: pd.DataFrame, band=0.05) -> dict:
    op = w["open"][target.columns]
    rets = op.shift(-1) / op - 1                     # open-to-open return earned by a weight set at this open
    fund = funding.reindex(index=op.index, columns=target.columns).fillna(0.0)
    pos = pd.DataFrame(0.0, index=op.index, columns=target.columns)
    cur = np.zeros(len(target.columns))
    eq = [1.0]
    liq = 0
    T = target.fillna(0.0).values
    R, F = rets.fillna(0.0).values, fund.values
    for t in range(1, len(op)):
        want = T[t - 1]
        change = np.abs(want - cur) > band
        change |= (want == 0) & (cur != 0)
        cost = np.abs(np.where(change, want - cur, 0)).sum() * COST
        cur = np.where(change, want, cur)
        r = R[t]
        # isolated margin: a coin's loss can't exceed the margin put up for it (its weight / leverage)
        pnl = cur * r
        margin = np.abs(cur) / LEV                      # equity set aside per coin
        hit = (pnl <= -margin * 0.95) & (cur != 0)      # liquidated (maintenance margin ~5%)
        pnl = np.where(hit, -margin, pnl)
        liq += int(hit.sum())
        cur = np.where(hit, 0.0, cur)
        pnl_total = pnl.sum() - (cur * F[t]).sum() - cost
        eq.append(eq[-1] * (1 + pnl_total))
        pos.iloc[t] = cur
    return {"equity": pd.Series(eq, index=op.index), "liquidations": liq, "pos": pos}


LEV = 1.0


def targets(w, mode: str, lev: float) -> pd.DataFrame:
    cl = w["close"][COINS]
    v = lab.vol(cl)
    scale = (0.5 / v).clip(upper=1.0)
    long_ = lab.donchian_ensemble(cl)
    if mode == "long only":
        t = long_ * scale / len(COINS)
    else:
        t = (long_ - short_votes(cl)) * scale / len(COINS)
    return t * lev


if __name__ == "__main__":
    d = Path(sys.argv[1])
    w = lab.load(d)
    f = pd.read_csv(d / "funding.csv.gz", parse_dates=["date"]).pivot(index="date", columns="symbol", values="funding")
    print(f"funding data from {f['BTC'].dropna().index[0]:%Y-%m}; before that funding is assumed 0.01%/8h for longs")
    f = f.reindex(w["close"].index)
    for c in COINS:
        f[c] = f[c].fillna(0.0003)
    btc = w["close"]["BTC"]
    print("Buy and hold BTC   H", lab.metrics(btc, "2023-01-01"), "D", lab.metrics(btc, "2018-01-01", "2022-12-31"))
    for mode in ("long only", "long/short"):
        for lev in (1.0, 2.0, 3.0):
            LEV = lev
            s = simulate(w, targets(w, mode, lev), f)
            e = s["equity"]
            last = e.index[-1] - pd.Timedelta(days=365)
            yrs = {y: lab.metrics(e, f"{y}-01-01", f"{y}-12-31").get("total") for y in range(2018, 2027)}
            print(f"{mode:10s} {lev:.0f}x  D {lab.metrics(e, '2018-01-01', '2022-12-31')}  H {lab.metrics(e, '2023-01-01')}  "
                  f"1y {lab.metrics(e, last).get('total')}  liq-days {s['liquidations']}\n      years {yrs}")
