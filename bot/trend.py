"""Trend signals for Bitcoin and Ethereum: the one rule set that passed testing.

Rule (from Zarattini, Pagani & Barbon 2025, "Catching Crypto Trends"; fixed
settings, not tuned to our data):
  * Eight trend "votes" per coin, one per lookback (10 ... 360 days).
  * A vote turns on when the daily close is the highest close of its lookback,
    and turns off when the close falls below a trailing stop: the midpoint of
    the highest and lowest close of that lookback (the stop only ever rises).
  * Position = share of votes that are on x min(1, 50% / the coin's yearly
    volatility), split equally between the coins. Never more than 100%.
  * Decided on the daily close (00:00 UTC), traded at the next open. A change
    is only signalled when it moves the position by 5% of the account or more.

Tested on Binance daily prices 2017-2026 with 0.15% cost per side: 2023-now
(never used to design it) +19.5%/yr, worst drop -23% (holding BTC: -53%).
"""
import math
import time
from datetime import datetime, timezone

LOOKBACKS = [10, 20, 30, 60, 90, 150, 250, 360]
COINS = ["BTC", "ETH"]
VOL_TARGET = 0.50
VOL_DAYS = 90
BAND = 0.05
COST = 0.0015


def votes(closes: list[float]) -> dict:
    """Runs every lookback through the whole history. Returns per-day share of
    votes on, plus today's state for each lookback."""
    n = len(closes)
    share = [0.0] * n
    state = []
    for lb in LOOKBACKS:
        on, stop = False, None
        for t in range(n):
            if t + 1 < lb:
                continue
            window = closes[t + 1 - lb:t + 1]
            hi, lo = max(window), min(window)
            mid = (hi + lo) / 2
            c = closes[t]
            if not on and c >= hi:
                on, stop = True, mid
            elif on:
                stop = max(stop, mid)
                if c <= stop:
                    on, stop = False, None
            if on:
                share[t] += 1 / len(LOOKBACKS)
        hi = max(closes[-lb:]) if n >= lb else None
        state.append({"lookback": lb, "on": on, "stop": stop, "trigger": hi})
    return {"share": share, "state": state}


def volatility(closes: list[float], days: int = VOL_DAYS) -> list[float | None]:
    """Yearly volatility from the last `days` daily returns (None until 30 exist)."""
    rets = [None] + [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
    out: list[float | None] = []
    for t in range(len(closes)):
        win = [r for r in rets[max(1, t - days + 1):t + 1] if r is not None]
        if len(win) < 30:
            out.append(None)
            continue
        m = sum(win) / len(win)
        sd = math.sqrt(sum((r - m) ** 2 for r in win) / (len(win) - 1))
        out.append(sd * math.sqrt(365))
    return out


def weights(history: dict[str, list[list]]) -> dict[str, dict]:
    """Per coin: daily target share of the account, and today's detail.
    history[coin] = [[date 'YYYY-MM-DD', open, close], ...] of completed days."""
    out = {}
    for coin in COINS:
        rows = history.get(coin, [])
        closes = [r[2] for r in rows]
        if len(closes) < 40:
            continue
        v = votes(closes)
        vol = volatility(closes)
        series = []
        for s, vv in zip(v["share"], vol):
            scale = min(1.0, VOL_TARGET / vv) if vv else 0.0
            series.append(s * scale / len(COINS))
        on = [s for s in v["state"] if s["on"]]
        off = [s for s in v["state"] if not s["on"] and s["trigger"]]
        out[coin] = {
            "dates": [r[0] for r in rows], "series": series,
            "target": series[-1], "votes_on": len(on), "votes": len(LOOKBACKS),
            "close": closes[-1], "as_of": rows[-1][0], "volatility": vol[-1],
            # a daily close below this sells part; below the lowest, all of it
            "next_trim": max((s["stop"] for s in on), default=None),
            "full_exit": min((s["stop"] for s in on), default=None),
            # a daily close at or above this adds to the position
            "next_add": min((s["trigger"] for s in off), default=None),
        }
    return out


def simulate(history: dict[str, list[list]], w: dict[str, dict], start: str | None = None) -> dict:
    """Paper-trades the signals: decide at the close, trade next open, 0.15% a
    side, rebalance only on moves of 5%+. Returns equity by date."""
    dates = sorted({d for c in w for d in w[c]["dates"] if not start or d >= start})
    if len(dates) < 2:
        return {"dates": dates, "equity": [1.0] * len(dates), "trades": 0}
    px = {c: {r[0]: (r[1], r[2]) for r in history[c]} for c in w}
    tgt = {c: dict(zip(w[c]["dates"], w[c]["series"])) for c in w}
    cash, units, eq, trades = 1.0, {c: 0.0 for c in w}, [1.0], 0
    last = {c: None for c in w}
    for i in range(1, len(dates)):
        d, prev = dates[i], dates[i - 1]
        opens = {c: px[c].get(d, (None, None))[0] or last[c] for c in w}
        equity = cash + sum(units[c] * (opens[c] or 0) for c in w)
        for c in w:
            if not opens[c]:
                continue
            want = tgt[c].get(prev, 0.0) * equity
            have = units[c] * opens[c]
            diff = want - have
            if abs(diff) > BAND * equity or (want == 0 and have > 0):
                if have == 0 and diff > 0:
                    trades += 1
                cash -= diff + abs(diff) * COST
                units[c] += diff / opens[c]
        for c in w:
            last[c] = px[c].get(d, (None, last[c]))[1] or last[c]
        eq.append(cash + sum(units[c] * (last[c] or 0) for c in w))
    return {"dates": dates, "equity": eq, "trades": trades}


def stats(dates: list[str], equity: list[float]) -> dict:
    if len(equity) < 2:
        return {"total": 0.0, "max_dd": 0.0, "drawdown": 0.0, "days": len(equity)}
    peak, dd = equity[0], 0.0
    for e in equity:
        peak = max(peak, e)
        dd = min(dd, e / peak - 1)
    return {"total": round((equity[-1] / equity[0] - 1) * 100, 1), "max_dd": round(dd * 100, 1),
            "drawdown": round((equity[-1] / peak - 1) * 100, 1), "days": len(equity) - 1}


# ---------- data ----------

def update_history(cache: dict, mkt, now: datetime | None = None) -> dict:
    """Adds the latest completed Bitget daily candles to the cached history."""
    now = now or datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    for coin in COINS:
        rows = {r[0]: r for r in cache.get(coin, [])}
        candles = mkt.candles(coin, "1d", 300)
        for c in candles:
            day = time.strftime("%Y-%m-%d", time.gmtime(c[0] / 1000))
            if day < today:              # skip today's unfinished candle
                rows[day] = [day, float(c[1]), float(c[4])]
        cache[coin] = [rows[d] for d in sorted(rows)]
    return cache


# ---------- signals ----------

# Safeguards. Tested worst drop since 2023 was -23%; falling well past that
# live means the market no longer behaves like the test, so stop buying.
PAUSE_DRAWDOWN = -35.0
STALE_DAYS = 2


def guard(live: dict, as_of: str, now: datetime, health: dict | None = None) -> dict:
    """Decides whether new buys are allowed. Sells always go out."""
    reasons = []
    if live.get("drawdown", 0.0) <= PAUSE_DRAWDOWN:
        reasons.append(f"live paper record is down {live['drawdown']:.0f}% from its peak, "
                       f"past the {PAUSE_DRAWDOWN:.0f}% safety limit")
    age = (now.date() - datetime.strptime(as_of, "%Y-%m-%d").date()).days
    if age > STALE_DAYS:
        reasons.append(f"prices are {age} days old")
    health_bad = (health or {}).get("status") == "weak"
    if health_bad:
        reasons.append("the weekly re-test shows the rule has stopped working "
                       f"(2-year Sharpe {health['sharpe_2y']:.2f})")
    return {"buys_allowed": not reasons, "reasons": reasons}


def signals(w: dict[str, dict], state: dict, account: float, buys_allowed: bool = True) -> list[str]:
    """Telegram messages when a coin's target moved 5%+ of the account since the
    last signal. `state` remembers the last announced targets. While paused,
    increases are held back (sells still go out)."""
    msgs = []
    sent = state.setdefault("announced", {})
    for coin, d in w.items():
        new, old = d["target"], sent.get(coin)
        if old is None:
            sent[coin] = new             # first run: remember silently
            continue
        if new > old and not buys_allowed:
            continue
        if abs(new - old) < BAND and not (new == 0 < old):
            continue
        sent[coin] = new
        msgs.append(signal_text(coin, d, old, account))
    return msgs


def _p(x: float | None) -> str:
    from .alerts import fmt_price
    if x is None:
        return "-"
    return f"{x:,.0f}" if x >= 100 else fmt_price(x)


def signal_text(coin: str, d: dict, old: float, account: float) -> str:
    new = d["target"]
    if new == 0:
        head = f"🔴 Trend signal: SELL all {coin}"
        why = f"{coin}'s daily close ({_p(d['close'])}) fell below the last trend stop. Hold no {coin}."
    elif new > old:
        head = f"🟢 Trend signal: BUY {coin}"
        why = f"{d['votes_on']} of {d['votes']} trend checks are now up (new highs)."
    else:
        head = f"🟠 Trend signal: SELL some {coin}"
        why = f"Only {d['votes_on']} of {d['votes']} trend checks are still up."
    lines = [head, "",
             f"Hold {new * 100:.0f}% of your account in {coin} (was {old * 100:.0f}%)",
             f"= about {new * account:,.0f} USDT of {account:,.0f}. Buy or sell at market on Bitget spot.",
             f"Why: {why}"]
    if d["full_exit"]:
        lines.append(f"Sell some on a daily close below {_p(d['next_trim'])}; all of it below {_p(d['full_exit'])}.")
    if d["next_add"]:
        lines.append(f"Add more on a daily close above {_p(d['next_add'])}.")
    lines += ["", "Tested rule (2023-now: +19%/yr, worst drop -23% vs -53% holding BTC). Not advice."]
    return "\n".join(lines)


def intro_text(w: dict[str, dict], account: float) -> str:
    lines = ["📊 Trend signals are now live (Bitcoin and Ethereum, Bitget spot)", "",
             "Where the rule says to be today:"]
    for coin, d in w.items():
        pct = d["target"] * 100
        lines.append(f"• {coin}: hold {pct:.0f}% of your account (about {d['target'] * account:,.0f} USDT); "
                     f"{d['votes_on']} of {d['votes']} trend checks up"
                     + (f"; all out below {_p(d['full_exit'])}" if d["full_exit"] else ""))
    lines += ["", "You'll get a message only when a position should change by 5% of your account or more. "
              "Checked once a day, just after the daily close (1am UK time in summer, midnight in winter).",
              "Tested 2017-2026: worst drop -23% since 2023 vs -53% holding Bitcoin. "
              "It earns less than holding in strong bull runs; its job is avoiding the big crashes. Not advice."]
    return "\n".join(lines)


def pause_text(reasons: list[str]) -> str:
    return ("⏸ Trend signals: new BUYS paused\n\nWhy: " + "; ".join(reasons) + ".\n\n"
            "Sell signals still come through. Buys restart on their own once this clears.")


def resume_text() -> str:
    return "▶️ Trend signals: buys are back on. The safety check is clear again."
