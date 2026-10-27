"""Tests two fixes suggested by the live losses of Oct 2026 (failed altcoin
breakout buys), against the live altcoin rule (max 6 coins, sell under SMA20).

  A  no new altcoin positions while the Bitcoin trend is being cut
     (BTC's share of trend votes is below its 10-day high)
  B  confirmation: only open a coin after a second daily close in the trend
     (the raw signal was already on the day before)
  A+B both
Usage: python research/loss_tests.py <research-data dir>
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lab  # noqa: E402
from alts import alt_universe  # noqa: E402


def sleeve(w, f, variant):
    cl = w["close"]
    btc = cl["BTC"]
    gate = lab.sma_gate(btc, 200, 0.02)
    uni = alt_universe(w, 20)
    names = [c for c in cl.columns if uni[c].any()]
    c_, u, v = cl[names], uni[names], lab.vol(cl)[names]
    raw = (lab.donchian_ensemble(c_).where(u, 0.0) * (0.5 / v).clip(upper=1.0)).clip(upper=1.0) / 6
    rk = raw.where(raw > 0).rank(axis=1, ascending=False)
    raw = raw.where(rk <= 6, 0.0)
    raw = raw.where(c_ > c_.rolling(20, min_periods=20).mean(), 0.0)
    btc_share = lab.donchian_ensemble(cl[["BTC"]])["BTC"]
    cutting = (btc_share < btc_share.rolling(10, min_periods=1).max()).values
    if variant != "live":
        R = raw.fillna(0).values
        O = np.zeros_like(R)
        for t in range(len(R)):
            prev = O[t - 1] if t else np.zeros(R.shape[1])
            opening = (prev == 0) & (R[t] > 0)
            block = np.zeros(R.shape[1], bool)
            if "A" in variant and cutting[t]:
                block |= opening
            if "B" in variant:
                block |= opening & ~((R[t - 1] > 0) if t else np.zeros(R.shape[1], bool))
            O[t] = np.where(block, 0.0, R[t])
        raw = pd.DataFrame(O, index=raw.index, columns=raw.columns)
    med = f.reindex(columns=raw.columns).where(raw > 0).median(axis=1).reindex(raw.index)
    hot = (med.rolling(7).mean() > 0.0009).astype(float)
    return raw.mul(gate, axis=0).mul(1 - 0.5 * hot, axis=0)


if __name__ == "__main__":
    d = Path(sys.argv[1])
    w = lab.load(d)
    f = pd.read_csv(d / "funding.csv.gz", parse_dates=["date"]).pivot(index="date", columns="symbol", values="funding")
    cl = w["close"]
    v = lab.vol(cl)
    core = lab.donchian_ensemble(cl[["BTC", "ETH"]]) * (0.5 / v[["BTC", "ETH"]]).clip(upper=1) / 2
    for name in ("live", "A", "B", "AB"):
        alt = sleeve(w, f, name)
        ea = lab.simulate(w, alt)["equity"]
        e = lab.simulate(w, core.mul(0.75).add(alt.mul(0.25), fill_value=0))["equity"]
        m = lambda s, a, b=None: lab.metrics(s, a, b) if b else lab.metrics(s, a)
        yrs = {y: lab.metrics(ea, f"{y}-01-01", f"{y}-12-31").get("total") for y in range(2019, 2027)}
        print(f"{name:4s} alts: design Sh {m(ea,'2018-01-01','2022-12-31').get('sharpe')} | 2023+ Sh {m(ea,'2023-01-01').get('sharpe')} "
              f"cagr {m(ea,'2023-01-01').get('cagr')} dd {m(ea,'2023-01-01').get('max_dd')} || combined 2023+ Sh "
              f"{m(e,'2023-01-01').get('sharpe')} cagr {m(e,'2023-01-01').get('cagr')} dd {m(e,'2023-01-01').get('max_dd')} "
              f"design Sh {m(e,'2018-01-01','2022-12-31').get('sharpe')}\n      alt years {yrs}", flush=True)
