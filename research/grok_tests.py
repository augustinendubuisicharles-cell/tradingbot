"""Tests Grok's suggested upgrades to the altcoin portion (2026-10-09), each on
its own, against the live rule. Same simulator, costs and periods as lab.py.

  V1 entry filter: only open a coin while it also beats BTC over 28 days
  V2 exit filter: only cut a coin when its trend breaks AND it lags BTC (28 days)
  V3 fewer names: hold at most 6 coins (the strongest), 1/6 cap each
  V4 stricter break: also sell when the daily close is under the 20-day average
Usage: python research/grok_tests.py <research-data dir>
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
    sig = lab.donchian_ensemble(c_).where(u, 0.0)
    k = 6 if variant == "V3" else 10
    raw = (sig * (0.5 / v).clip(upper=1.0)).clip(upper=1.0) / k
    rel = c_.div(btc, axis=0).pct_change(28, fill_method=None)
    if variant == "V1":                       # no new position unless beating BTC
        out = raw.copy()
        R, REL = raw.values, rel.values
        O = np.zeros_like(R)
        for t in range(len(R)):
            prev = O[t - 1] if t else np.zeros(R.shape[1])
            opening = (prev == 0) & (R[t] > 0)
            O[t] = np.where(opening & ~(REL[t] > 0), 0.0, R[t])
        raw = pd.DataFrame(O, index=raw.index, columns=raw.columns)
    elif variant == "V2":                     # only reduce when also lagging BTC
        R, REL = raw.values, rel.values
        O = np.zeros_like(R)
        for t in range(len(R)):
            prev = O[t - 1] if t else np.zeros(R.shape[1])
            cut = R[t] < prev
            keep = cut & (REL[t] > 0) & u.values[t]
            O[t] = np.where(keep, prev, R[t])
        raw = pd.DataFrame(O, index=raw.index, columns=raw.columns)
    elif variant == "V3":                     # strongest 6 only
        rk = raw.where(raw > 0).rank(axis=1, ascending=False)
        raw = raw.where(rk <= 6, 0.0)
    elif variant == "V4":
        raw = raw.where(c_ > c_.rolling(20, min_periods=20).mean(), 0.0)
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
    for name in ("live rule", "V1", "V2", "V3", "V4"):
        alt = sleeve(w, f, name)
        s_alt = lab.simulate(w, alt)
        combo = core.mul(0.75).add(alt.mul(0.25), fill_value=0)
        s = lab.simulate(w, combo)
        e, ea = s["equity"], s_alt["equity"]
        names_held = (alt > 0).sum(axis=1).loc["2023":].mean()
        print(f"{name:9s} portion alone: D {lab.metrics(ea, '2018-01-01', '2022-12-31').get('sharpe')} "
              f"H sh {lab.metrics(ea, '2023-01-01').get('sharpe')} dd {lab.metrics(ea, '2023-01-01').get('max_dd')} "
              f"cagr {lab.metrics(ea, '2023-01-01').get('cagr')} | with BTC/ETH: H sh {lab.metrics(e, '2023-01-01').get('sharpe')} "
              f"dd {lab.metrics(e, '2023-01-01').get('max_dd')} cagr {lab.metrics(e, '2023-01-01').get('cagr')} "
              f"D sh {lab.metrics(e, '2018-01-01', '2022-12-31').get('sharpe')} | avg coins {names_held:.1f} "
              f"turnover/yr {s_alt['turnover'] / (len(ea) / 365):.1f}", flush=True)
