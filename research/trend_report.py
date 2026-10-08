"""Writes data/trend_backtest.json: the tested results the dashboard shows for
the trend signals. Usage: python research/trend_report.py <research-data dir>"""
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import lab  # noqa: E402
from bot import trend  # noqa: E402

d = Path(sys.argv[1])
w = lab.load(d)
hist = {c: [[i.strftime("%Y-%m-%d"), float(o), float(c_)] for i, o, c_ in
             zip(w["close"].index, w["open"][c], w["close"][c]) if pd.notna(c_)] for c in trend.COINS}
wt = trend.weights(hist)
sim = trend.simulate(hist, wt)
eq = pd.Series(sim["equity"], index=pd.to_datetime(sim["dates"]))
btc = w["close"]["BTC"].dropna()
last = eq.index[-1] - pd.Timedelta(days=365)
out = {
    "generated_at": pd.Timestamp.now("UTC").isoformat(),
    "data": f"Binance daily prices {eq.index[0]:%b %Y} to {eq.index[-1]:%b %Y}, 0.15% cost per side",
    "periods": [
        {"name": "Designed on (2018-2022)", "trend": lab.metrics(eq, "2018-01-01", "2022-12-31"), "btc": lab.metrics(btc, "2018-01-01", "2022-12-31")},
        {"name": "Never seen (2023-now)", "trend": lab.metrics(eq, "2023-01-01"), "btc": lab.metrics(btc, "2023-01-01")},
        {"name": "Last 12 months", "trend": lab.metrics(eq, last), "btc": lab.metrics(btc, last)},
    ],
    "by_year": [{"year": y, "trend": lab.metrics(eq, f"{y}-01-01", f"{y}-12-31").get("total"),
                 "btc": lab.metrics(btc, f"{y}-01-01", f"{y}-12-31").get("total")} for y in range(2018, eq.index[-1].year + 1)],
    "trades_per_year": round(sim["trades"] / (len(eq) / 365), 1),
    "exposure_pct": round(float(pd.DataFrame({c: wt[c]["series"] for c in wt}).sum(axis=1).mean() * 100), 0),
}
out = json.loads(json.dumps(out, default=float))
Path("data/trend_backtest.json").write_text(json.dumps(out, indent=2))
print(json.dumps(out, indent=1))
