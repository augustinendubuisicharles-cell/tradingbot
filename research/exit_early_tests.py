"""Tests "sell before the crowd": trim the BTC/ETH trend position when the
market looks overheated, then let the normal rule buy back.

  G80/G90   hold half while Fear & Greed is at or above 80 / 90
  F         hold half while BTC funding (7-day average) is above 0.09%/day
  G80+F     either
Usage: python research/exit_early_tests.py <research-data dir>
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lab  # noqa: E402

if __name__ == "__main__":
    d = Path(sys.argv[1])
    w = lab.load(d)
    cl = w["close"]
    v = lab.vol(cl)
    core = lab.donchian_ensemble(cl[["BTC", "ETH"]]) * (0.5 / v[["BTC", "ETH"]]).clip(upper=1) / 2
    fng = pd.read_csv(d / "fng.csv", parse_dates=["date"]).set_index("date")["value"].reindex(cl.index).ffill()
    f = pd.read_csv(d / "funding.csv.gz", parse_dates=["date"]).pivot(index="date", columns="symbol", values="funding")
    fund = f["BTC"].reindex(cl.index).rolling(7).mean()
    tests = {"live": pd.Series(False, index=cl.index), "G80": fng >= 80, "G90": fng >= 90,
             "F": fund > 0.0009, "G80+F": (fng >= 80) | (fund > 0.0009)}
    for name, hot in tests.items():
        t = core.mul(1 - 0.5 * hot.astype(float), axis=0)
        e = lab.simulate(w, t)["equity"]
        m = lambda a, b=None: lab.metrics(e, a, b) if b else lab.metrics(e, a)
        yrs = {y: lab.metrics(e, f"{y}-01-01", f"{y}-12-31").get("total") for y in range(2018, 2027)}
        print(f"{name:6s} days hot {int(hot.sum()):4d} | 2018-22 Sh {m('2018-02-01','2022-12-31').get('sharpe')} "
              f"cagr {m('2018-02-01','2022-12-31').get('cagr')} dd {m('2018-02-01','2022-12-31').get('max_dd')} | "
              f"2023+ Sh {m('2023-01-01').get('sharpe')} cagr {m('2023-01-01').get('cagr')} dd {m('2023-01-01').get('max_dd')}"
              f"\n       years {yrs}", flush=True)
