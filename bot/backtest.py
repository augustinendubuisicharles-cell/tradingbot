"""Backtest: replays the hidden-gem timing rules on the past year of Bitget 4h
prices, tries setting variations, and keeps a variation only if it also wins
on the most recent months it was not tuned on (to avoid fooling ourselves).

Only the price rules can be replayed: safety checks, social buzz and narratives
have no free history, so those are judged by the live track record instead.
"""
import itertools
import logging
import re
import time
from datetime import datetime, timedelta, timezone

from . import analysis
from .emerging import DEFAULT_TIMING, plan_stop
from .tracker import manage_trade

log = logging.getLogger(__name__)
NOT_COINS = re.compile(r"^(USDC|USDE|FDUSD|DAI|TUSD|USDD|PYUSD|EUR\w*|GBP|BUSD|USD\w*|"
                       r"XAUT|PAXG|WBTC|STETH|WETH|BTCDOM)$|\d[LS]$|(UP|DOWN|BULL|BEAR)$")
GRID = {
    "trend_min": [55, 75],
    "rsi_max": [65, 70],
    "stop_atr": [1.5, 2.5, 3.5],
    "tp": [(1.5, 3.0), (2.0, 4.0), (3.0, 6.0)],
    "exit_on_ema20": [True, False],
}


# ---------- data ----------

def pick_symbols(mkt, extra: list[str], n: int = 100, min_volume: float = 1e6) -> list[tuple[str, float]]:
    """The n most-traded Bitget USDT spot coins (no stablecoins or leveraged tokens), plus extras."""
    mkt.spot.load_markets()
    tickers = mkt.spot.fetch_tickers()
    rows = []
    for sym, t in tickers.items():
        if not sym.endswith(f"/{mkt.quote}") or ":" in sym:
            continue
        coin = sym.split("/")[0]
        vol = float(t.get("quoteVolume") or 0)
        if NOT_COINS.search(coin) or vol < min_volume:
            continue
        rows.append((coin, vol))
    rows.sort(key=lambda r: r[1], reverse=True)
    picked = rows[:n]
    have = {c for c, _ in picked}
    vols = dict(rows)
    picked += [(c, vols.get(c, 0.0)) for c in extra if c not in have and f"{c}/{mkt.quote}" in mkt.spot.markets]
    return picked


def history(mkt, coin: str, days: int = 365) -> list[list[float]]:
    """About a year of 4h candles, fetched page by page."""
    now_ms = int(time.time() * 1000)
    since = now_ms - days * 86400_000
    out: list[list[float]] = []
    while since < now_ms - 4 * 3600_000:
        rows = mkt.spot.fetch_ohlcv(mkt.spot_symbol(coin), "4h", since=since, limit=1000)
        rows = [r for r in rows if not out or r[0] > out[-1][0]]
        if not rows:
            break
        out += rows
        since = rows[-1][0] + 1
    return out


# ---------- indicators for every candle (no peeking ahead) ----------

def indicators(c4h: list) -> dict:
    closes = [c[4] for c in c4h]
    n = len(c4h)
    rsi = [None] * n
    g = l = 0.0
    for i in range(1, n):
        d = closes[i] - closes[i - 1]
        if i <= 14:
            g += max(d, 0); l += max(-d, 0)
            if i == 14:
                g, l = g / 14, l / 14
                rsi[i] = 100.0 if l == 0 else 100 - 100 / (1 + g / l)
        else:
            g = (g * 13 + max(d, 0)) / 14
            l = (l * 13 + max(-d, 0)) / 14
            rsi[i] = 100.0 if l == 0 else 100 - 100 / (1 + g / l)
    atr = [None] * n
    trs = [0.0] + [max(c4h[i][2] - c4h[i][3], abs(c4h[i][2] - closes[i - 1]), abs(c4h[i][3] - closes[i - 1]))
                   for i in range(1, n)]
    for i in range(14, n):
        atr[i] = sum(trs[1:15]) / 14 if i == 14 else (atr[i - 1] * 13 + trs[i]) / 14
    # Daily trend from completed UTC days only.
    day_close, day_idx = [], [None] * n
    for i, c in enumerate(c4h):
        day = int(c[0] // 86400_000)
        if i + 1 == n or int(c4h[i + 1][0] // 86400_000) != day:
            day_close.append(c[4])
        day_idx[i] = len(day_close) - 1  # the latest day that has closed by this candle
    d50 = analysis.ema(day_close, 50)
    return {"closes": closes, "e20": analysis.ema(closes, 20), "e50": analysis.ema(closes, 50),
            "rsi": rsi, "atr": atr, "day_close": day_close, "d50": d50, "day_idx": day_idx}


def trend_at(ind: dict, i: int) -> float:
    c, e20, e50 = ind["closes"][i], ind["e20"][i], ind["e50"][i]
    s = 0.0
    if e50 is not None and c > e50:
        s += 25
    if e20 is not None and e50 is not None and e20 > e50:
        s += 25
    d = ind["day_idx"][i]
    if d is not None and d >= 0 and ind["d50"][d] is not None and ind["day_close"][d] > ind["d50"][d]:
        s += 30
    if i >= 6 and e50 is not None and ind["e50"][i - 6] is not None and e50 > ind["e50"][i - 6]:
        s += 20
    return s


def signal_at(c4h: list, ind: dict, i: int, p: dict) -> dict | None:
    """The live timing rules at candle i: a market buy ("Enter zone") or a limit buy ("Wait for pullback")."""
    a, e20, e50, r = ind["atr"][i], ind["e20"][i], ind["e50"][i], ind["rsi"][i]
    if a is None or e20 is None or e50 is None or r is None or i < 60:
        return None
    price, trend = ind["closes"][i], trend_at(ind, i)
    if trend < p["trend_min"] or r >= 78:
        return None
    prev_r = ind["rsi"][i - 1]
    if e20 > e50 and price < e20 and prev_r is not None and r < 50 and r < prev_r:
        return None
    if r <= p["rsi_max"] and price - e20 <= p["stretch_atr"] * a:
        entry, limit = price, False
    else:
        entry, limit = e20, True
    swing_low = min(c[3] for c in c4h[i - 9:i + 1])
    stop = plan_stop(entry, swing_low, a, p["stop_atr"], not limit)
    risk = entry - stop
    if risk <= 0:
        return None
    return {"entry": entry, "stop": stop, "tp1": entry + p["tp1_r"] * risk,
            "tp2": entry + p["tp2_r"] * risk, "limit": limit}


# ---------- simulation ----------

def run_symbol(c4h: list, ind: dict, p: dict) -> list[dict]:
    trades, i, n = [], 60, len(c4h)
    while i < n - 1:
        s = signal_at(c4h, ind, i, p)
        if not s:
            i += 1
            continue
        res = manage_trade(c4h, i, s["entry"], s["stop"], s["tp1"], s["tp2"], limit=s["limit"],
                           exit_on_ema20=p["exit_on_ema20"], e20=ind["e20"])
        if "result_r" not in res:  # still running at the end of the data
            break
        if res["status"] != "never filled":
            trades.append({"ts": c4h[i][0], "r": res["result_r"], "status": res["status"],
                           "bars": res["bars"], "limit": s["limit"], "hit_tp1": res["half_taken"]})
        i = res["closed_i"] + 1
    return trades


def stats(trades: list[dict]) -> dict:
    if not trades:
        return {"trades": 0}
    rs = [t["r"] for t in sorted(trades, key=lambda t: t["ts"])]
    wins = [r for r in rs if r > 0]
    losses = [-r for r in rs if r < 0]
    peak = cum = dd = 0.0
    for r in rs:
        cum += r
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return {"trades": len(rs), "win_rate": round(100 * len(wins) / len(rs)),
            "avg_r": round(sum(rs) / len(rs), 3), "total_r": round(sum(rs), 1),
            "profit_factor": round(sum(wins) / sum(losses), 2) if losses else None,
            "max_drawdown_r": round(dd, 1),
            "target1_rate": round(100 * sum(t["hit_tp1"] for t in trades) / len(rs)),
            "avg_days": round(sum(t["bars"] for t in trades) / len(trades) / 6, 1)}


def variants() -> list[dict]:
    keys = list(GRID)
    out = []
    for combo in itertools.product(*(GRID[k] for k in keys)):
        v = dict(zip(keys, combo))
        tp1, tp2 = v.pop("tp")
        out.append({**DEFAULT_TIMING, **v, "tp1_r": tp1, "tp2_r": tp2})
    return out


def evaluate(data: dict[str, tuple[list, dict, float]], split_ts: float, small_names: set[str]) -> dict:
    """Runs every variant; returns stats on the tuning months (train) and the recent months (test)."""
    results = []
    for p in [DEFAULT_TIMING] + variants():
        train, test, small = [], [], []
        for coin, (c4h, ind, _vol) in data.items():
            for t in run_symbol(c4h, ind, p):
                (train if t["ts"] < split_ts else test).append(t)
                if coin in small_names:
                    small.append(t)
        results.append({"params": p, "train": stats(train), "test": stats(test), "small_coins": stats(small)})
    return {"default": results[0], "variants": results[1:]}


def choose(ev: dict, min_train: int = 40, min_test: int = 15) -> dict:
    """Best variant on the tuning months, adopted only if it also beats the current
    settings on the recent months and makes money there."""
    ok = [v for v in ev["variants"] if v["train"]["trades"] >= min_train]
    if not ok:
        return {"adopted": False, "reason": "not enough trades to tune on", "params": DEFAULT_TIMING}
    best = max(ok, key=lambda v: v["train"]["avg_r"])
    d, b = ev["default"]["test"], best["test"]
    if b["trades"] < min_test:
        return {"adopted": False, "reason": "too few recent trades to confirm the better settings",
                "params": DEFAULT_TIMING, "candidate": best}
    if b["avg_r"] > max(0.0, d.get("avg_r") or -9) and best["params"] != DEFAULT_TIMING:
        return {"adopted": True, "reason": "better on the tuning months and on the recent months",
                "params": best["params"], "candidate": best}
    return {"adopted": False, "reason": "no variation beat the current settings on recent months",
            "params": DEFAULT_TIMING, "candidate": best}


def run(cfg: dict, mkt, extra: list[str], days: int = 365, test_days: int = 90) -> tuple[dict, dict]:
    started = time.time()
    syms = pick_symbols(mkt, extra)
    data = {}
    for coin, vol in syms:
        try:
            c4h = history(mkt, coin, days)
        except Exception as e:  # noqa: BLE001
            log.warning("history unavailable for %s: %s", coin, e)
            continue
        if len(c4h) >= 300:
            data[coin] = (c4h, indicators(c4h), vol)
    by_vol = sorted(data, key=lambda c: data[c][2], reverse=True)
    small = set(by_vol[30:])  # outside the 30 most-traded: closest to "gem" size
    split = (datetime.now(timezone.utc) - timedelta(days=test_days)).timestamp() * 1000
    ev = evaluate(data, split, small)
    choice = choose(ev)
    top = sorted((v for v in ev["variants"] if v["train"]["trades"] >= 40),
                 key=lambda v: v["train"]["avg_r"], reverse=True)[:5]
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "coins": len(data), "days": days, "test_days": test_days,
        "candles": sum(len(v[0]) for v in data.values()),
        "default": ev["default"], "best": top, "choice": choice,
        "seconds": round(time.time() - started),
    }
    tuned = {"generated_at": report["generated_at"], "adopted": choice["adopted"],
             "params": choice["params"], "reason": choice["reason"]}
    return report, tuned


def summary_text(report: dict) -> str:
    d, c = report["default"], report["choice"]
    t, s = d["test"], d["small_coins"]
    lines = ["🧪 <b>Weekly accuracy check (backtest)</b>",
             f"Replayed the buy rules on {report['coins']} Bitget coins over the past {report['days']} days.",
             "",
             "<b>Current settings, last 90 days</b>"]
    if t.get("trades"):
        lines += [f"• {t['trades']} trades, {t['win_rate']}% made money",
                  f"• Average {t['avg_r']:+.2f}R per trade (R = the amount risked), total {t['total_r']:+.1f}R",
                  f"• Worst losing streak: −{t['max_drawdown_r']}R · average hold {t['avg_days']} days"]
    else:
        lines.append("• No trades in this period.")
    if s.get("trades"):
        lines.append(f"• Smaller coins only, whole year: {s['win_rate']}% won, {s['avg_r']:+.2f}R average")
    lines.append("")
    if c["adopted"]:
        p = c["params"]
        ct = c["candidate"]["test"]
        lines += ["✅ <b>Switched to better settings</b>",
                  f"Last 90 days: {ct['win_rate']}% won, {ct['avg_r']:+.2f}R average "
                  f"(was {t.get('avg_r', 0):+.2f}R).",
                  f"Stop {p['stop_atr']}× the average 4h move, targets at {p['tp1_r']}R and {p['tp2_r']}R, "
                  f"trend score at least {p['trend_min']}, RSI at most {p['rsi_max']}"
                  + (", exit on a close below the 20-period average." if p["exit_on_ema20"] else ".")]
    else:
        lines.append(f"Settings unchanged: {c['reason']}.")
    lines.append("\n<i>Past results don't guarantee future ones; fees are included.</i>")
    return "\n".join(lines)
