"""Overfitting checks over every strategy variant tried.

* Walk-forward: each January, pick the variant with the best Sharpe over the
  previous 2 years, hold it for the year. Shows what "always use the recent
  winner" would really have earned.
* PBO (Bailey et al., CSCV): split history into 16 blocks; for every way of
  using half as "training", see whether the training winner ranks below the
  median in the other half. PBO = share of times it did (lower is better).
* Deflated Sharpe ratio (Bailey & Lopez de Prado): probability that a
  strategy's Sharpe beats what the best of N random tries would show by luck.
"""
import itertools
import math
import sys

import numpy as np
import pandas as pd
from statistics import NormalDist

norm = NormalDist()


def sharpe(r):
    r = r.dropna()
    return r.mean() / r.std() * math.sqrt(365) if len(r) > 30 and r.std() > 0 else 0.0


def walk_forward(rets: pd.DataFrame, train_years=2, start=2020):
    out = []
    for y in range(start, rets.index[-1].year + 1):
        tr = rets.loc[f"{y - train_years}-01-01":f"{y - 1}-12-31"]
        best = max(rets.columns, key=lambda c: sharpe(tr[c]))
        te = rets.loc[f"{y}-01-01":f"{y}-12-31", best]
        out.append({"year": y, "picked": best, "return": round(((1 + te.fillna(0)).prod() - 1) * 100, 1),
                    "sharpe": round(sharpe(te), 2)})
    return out


def pbo(rets: pd.DataFrame, blocks=16):
    r = rets.dropna(how="all").fillna(0.0)
    parts = np.array_split(np.arange(len(r)), blocks)
    lam = []
    for combo in itertools.combinations(range(blocks), blocks // 2):
        tr_idx = np.concatenate([parts[i] for i in combo])
        te_idx = np.concatenate([parts[i] for i in range(blocks) if i not in combo])
        tr, te = r.iloc[tr_idx], r.iloc[te_idx]
        s_tr = tr.mean() / tr.std()
        s_te = te.mean() / te.std()
        best = s_tr.idxmax()
        rank = (s_te < s_te[best]).sum() / (len(s_te) - 1)     # 0..1, 1 = best out of sample
        rank = min(max(rank, 1e-6), 1 - 1e-6)
        lam.append(math.log(rank / (1 - rank)))
    return round(float(np.mean(np.array(lam) <= 0)), 2)


def deflated_sharpe(r: pd.Series, n_trials: int, trial_sharpes: list[float]) -> float:
    r = r.dropna()
    T = len(r)
    sr = r.mean() / r.std()                                   # per-period Sharpe
    var_trials = np.var([s / math.sqrt(365) for s in trial_sharpes])
    emc = 0.5772156649
    sr0 = math.sqrt(var_trials) * ((1 - emc) * norm.inv_cdf(1 - 1 / n_trials) + emc * norm.inv_cdf(1 - 1 / (n_trials * math.e)))
    g3, g4 = r.skew(), r.kurt() + 3
    z = (sr - sr0) * math.sqrt(T - 1) / math.sqrt(1 - g3 * sr + (g4 - 1) / 4 * sr ** 2)
    return round(float(norm.cdf(z)), 3)


if __name__ == "__main__":
    rets = pd.read_pickle(sys.argv[1])
    print("walk-forward (pick last 2 years' best each January):")
    for row in walk_forward(rets):
        print("  ", row)
    print("PBO over", rets.shape[1], "variants:", pbo(rets.loc["2018-01-01":]))
    sh = [sharpe(rets[c].loc["2018-01-01":]) for c in rets]
    for c in rets:
        print(f"  DSR {deflated_sharpe(rets[c].loc['2018-01-01':], rets.shape[1], sh):.3f}  full Sharpe {sharpe(rets[c].loc['2018-01-01':]):.2f}  {c}")
