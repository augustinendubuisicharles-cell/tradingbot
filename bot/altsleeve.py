"""Altcoin trend side portion: the one altcoin rule that held up in testing.

Rule (same trend votes as bot/trend.py, applied to altcoins):
  * Universe each day: the 20 most-traded altcoins (30-day median USDT volume
    >= $2M, at least a year of history; no BTC, ETH or stablecoins).
  * Each coin's weight = share of its 8 trend votes that are on
    x min(1, 50% / yearly volatility), at most 1/10 of the portion.
  * Only while Bitcoin is above its 200-day average (2% band).
  * Halved while the held coins' perpetual funding averages above 0.09% a day
    over 7 days (traders paying a lot to be long often comes before a crash).
  * The portion is 25% of the account; Bitcoin/Ethereum signals use the rest.

Tested 2018-2026 (research/alts.py, alts2.py): as a 25% portion next to the
BTC/ETH rule, 2023-now worst drop -18% instead of -23%, about +17%/yr.
"""
import re
import time
from datetime import datetime, timedelta, timezone

from . import trend

SLEEVE = 0.25
TOP = 20
CAP = 1 / 10
MIN_AGE = 365
MIN_VOL = 2e6
FUNDING_HOT = 0.0009
BAND = 0.01                     # message when a coin moves 1% of the account or more
NOT_ALTS = re.compile(r"^(BTC|ETH|USDC|USDE|FDUSD|DAI|TUSD|USDD|PYUSD|EUR\w*|GBP|BUSD|USD\w*|"
                      r"XAUT|PAXG|WBTC|STETH|WETH|WBETH|BFUSD|BTCDOM)$|\d[LS]$|(UP|DOWN|BULL|BEAR)$")


# ---------- data ----------

def update_history(cache: dict, mkt, now: datetime | None = None, candidates: int = 40) -> dict:
    """cache = {"coins": {coin: [[date, close, quote_volume], ...]}, "first": {coin: date}}.
    Refreshes the most-traded Bitget altcoins with their completed daily candles."""
    now = now or datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    coins, first = cache.setdefault("coins", {}), cache.setdefault("first", {})
    mkt.spot.load_markets()
    tickers = mkt.spot.fetch_tickers()
    rows = []
    for sym, t in tickers.items():
        if not sym.endswith(f"/{mkt.quote}") or ":" in sym:
            continue
        coin = sym.split("/")[0]
        if not NOT_ALTS.search(coin):
            rows.append((coin, float(t.get("quoteVolume") or 0)))
    rows.sort(key=lambda r: r[1], reverse=True)
    for coin, _ in rows[:candidates]:
        have = coins.get(coin, [])
        if have and have[-1][0] >= (now - timedelta(days=1)).strftime("%Y-%m-%d"):
            continue                                    # already has yesterday
        try:
            candles = mkt.candles(coin, "1d", 300)
        except Exception:  # noqa: BLE001 - one coin shouldn't stop the rest
            continue
        merged = {r[0]: r for r in have}
        for c in candles:
            day = time.strftime("%Y-%m-%d", time.gmtime(c[0] / 1000))
            if day < today:
                merged[day] = [day, float(c[4]), float(c[5]) * float(c[4])]
        coins[coin] = [merged[d] for d in sorted(merged)][-420:]
        first.setdefault(coin, coins[coin][0][0])
    return cache


# ---------- signal ----------

def _median(xs):
    xs = sorted(xs)
    n = len(xs)
    return (xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2) if n else 0.0


def btc_gate(btc_closes: list[float], n: int = 200, band: float = 0.02) -> bool:
    on = False
    for t in range(n - 1, len(btc_closes)):
        m = sum(btc_closes[t - n + 1:t + 1]) / n
        c = btc_closes[t]
        if c > m * (1 + band):
            on = True
        elif c < m * (1 - band):
            on = False
    return on


def universe(cache: dict, as_of: str) -> list[str]:
    ranked = []
    for coin, rows in cache.get("coins", {}).items():
        if not rows or rows[-1][0] < as_of:
            continue
        first = cache.get("first", {}).get(coin, rows[0][0])
        age = (datetime.strptime(as_of, "%Y-%m-%d") - datetime.strptime(first, "%Y-%m-%d")).days + 1
        vol = _median([r[2] for r in rows[-30:]])
        if age >= MIN_AGE and vol >= MIN_VOL and len(rows) >= 40:
            ranked.append((vol, coin))
    return [c for _, c in sorted(ranked, reverse=True)[:TOP]]


def weights(cache: dict, btc_closes: list[float], as_of: str, funding: dict[str, float | None] | None = None) -> dict:
    """Target share of the WHOLE account per altcoin (already x 25%)."""
    gate = btc_gate(btc_closes)
    coins = {}
    for coin in universe(cache, as_of):
        closes = [r[1] for r in cache["coins"][coin]]
        v = trend.votes(closes)
        vol = trend.volatility(closes)[-1]
        share = v["share"][-1]
        w = min(1.0, share * min(1.0, trend.VOL_TARGET / vol)) * CAP if vol else 0.0
        on = [s for s in v["state"] if s["on"]]
        coins[coin] = {"raw": w, "votes_on": len(on), "close": closes[-1],
                       "full_exit": min((s["stop"] for s in on), default=None)}
    held = [c for c, d in coins.items() if d["raw"] > 0]
    rates = [funding.get(c) for c in held if funding and funding.get(c) is not None] if funding else []
    hot = bool(rates) and _median(rates) > FUNDING_HOT
    scale = SLEEVE * (0.5 if hot else 1.0) * (1.0 if gate else 0.0)
    for d in coins.values():
        d["target"] = d["raw"] * scale
    return {"coins": coins, "gate": gate, "funding_hot": hot, "funding_median": _median(rates) if rates else None,
            "total": sum(d["target"] for d in coins.values()), "as_of": as_of}


def changes(w: dict, state: dict, account: float) -> str | None:
    """One daily summary of altcoin moves of 1%+ of the account (or full exits)."""
    sent = state.setdefault("alt_announced", {})
    first = not sent and not state.get("alt_started")
    state["alt_started"] = True
    lines = []
    for coin in sorted(set(sent) | set(w["coins"])):
        new = w["coins"].get(coin, {}).get("target", 0.0)
        old = sent.get(coin, 0.0)
        if abs(new - old) < BAND and not (new == 0 < old):
            continue
        sent[coin] = new
        if new == 0:
            sent.pop(coin)
        if first:
            continue
        verb = "BUY" if new > old else ("SELL all" if new == 0 else "SELL some")
        lines.append(f"• {verb} {coin}: hold {new * 100:.1f}% (was {old * 100:.1f}%) "
                     f"= {new * account:,.0f} USDT")
    if first:
        return intro_text(w, account)
    if not lines:
        return None
    head = "🪙 Altcoin trend portion: today's changes"
    notes = []
    if not w["gate"]:
        notes.append("Bitcoin is below its 200-day average, so the altcoin portion is in cash.")
    if w["funding_hot"]:
        notes.append("Traders are paying a lot to be long, so the portion is halved.")
    return "\n".join([head, ""] + lines + ([""] + notes if notes else []) +
                     ["", "Buy or sell at market on Bitget spot. Tested rule, not advice."])


def intro_text(w: dict, account: float) -> str:
    held = sorted(((d["target"], c) for c, d in w["coins"].items() if d["target"] > 0), reverse=True)
    lines = ["🪙 Altcoin trend portion is now live (25% of your account)", ""]
    if not w["gate"]:
        lines.append("Bitcoin is below its 200-day average, so this portion stays in cash for now.")
    elif not held:
        lines.append("No altcoin is in an uptrend right now, so this portion stays in cash.")
    else:
        lines.append(f"Hold {w['total'] * 100:.0f}% of your account across {len(held)} altcoins:")
        lines += [f"• {c}: {t * 100:.1f}% = {t * account:,.0f} USDT" for t, c in held]
    lines += ["", "You'll get one daily summary when a coin should change by 1% of your account or more."]
    return "\n".join(lines)


def paper(state: dict, w: dict, cache: dict) -> dict:
    """Live paper record of the portion, from daily snapshots of its targets
    (close-to-close returns, 0.15% cost on each change)."""
    rec = state.setdefault("alt_paper", {"equity": 1.0, "peak": 1.0, "last": None, "targets": {}, "start": w["as_of"]})
    if rec["last"] and w["as_of"] > rec["last"]:
        px = {c: {r[0]: r[1] for r in rows[-10:]} for c, rows in cache["coins"].items()}
        growth = 0.0
        for coin, t in rec["targets"].items():
            p0, p1 = px.get(coin, {}).get(rec["last"]), px.get(coin, {}).get(w["as_of"])
            if p0 and p1:
                growth += t / SLEEVE * (p1 / p0 - 1)
        new = {c: d["target"] for c, d in w["coins"].items() if d["target"] > 0}
        turnover = sum(abs(new.get(c, 0) - rec["targets"].get(c, 0)) for c in set(new) | set(rec["targets"])) / SLEEVE
        rec["equity"] *= (1 + growth) * (1 - turnover * trend.COST)
        rec["peak"] = max(rec["peak"], rec["equity"])
    if rec["last"] != w["as_of"]:
        rec["targets"] = {c: d["target"] for c, d in w["coins"].items() if d["target"] > 0}
        rec["last"] = w["as_of"]
    return {"start": rec["start"], "total": round((rec["equity"] - 1) * 100, 1),
            "drawdown": round((rec["equity"] / rec["peak"] - 1) * 100, 1)}
