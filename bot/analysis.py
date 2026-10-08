"""Turns market data and sentiment into a per-coin score and spot trade ideas.

Spot only: in the UK, crypto futures are banned for retail traders, so futures
data is used purely as a crowd-positioning gauge.
"""
from datetime import datetime, timedelta, timezone

WEIGHTS = {"trend": 0.35, "momentum": 0.15, "sentiment": 0.25, "positioning": 0.10, "risk": 0.15}


# ---------- indicators ----------

def ema(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if len(values) < period:
        return out
    k = 2 / (period + 1)
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) <= period:
        return None
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    avg_g, avg_l = gains / period, losses / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_g = (avg_g * (period - 1) + max(d, 0)) / period
        avg_l = (avg_l * (period - 1) + max(-d, 0)) / period
    if avg_l == 0:
        return 100.0
    return 100 - 100 / (1 + avg_g / avg_l)


def atr(candles: list[list[float]], period: int = 14) -> float | None:
    if len(candles) <= period:
        return None
    trs = []
    for i in range(1, len(candles)):
        _, _, high, low, _, _ = candles[i][:6]
        prev_close = candles[i - 1][4]
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    value = sum(trs[:period]) / period
    for tr in trs[period:]:
        value = (value * (period - 1) + tr) / period
    return value


# ---------- component scores (each 0..100) ----------

def trend_score(c4h: list[list[float]], c1d: list[list[float]]) -> tuple[float, list[str]]:
    closes = [c[4] for c in c4h]
    e20, e50 = ema(closes, 20), ema(closes, 50)
    d_closes = [c[4] for c in c1d]
    d50 = ema(d_closes, 50)
    score, notes = 0.0, []
    if e50[-1] is not None and closes[-1] > e50[-1]:
        score += 25
        notes.append("price above 4h EMA50")
    if e20[-1] is not None and e50[-1] is not None and e20[-1] > e50[-1]:
        score += 25
        notes.append("4h EMA20 above EMA50")
    if d50[-1] is not None and d_closes[-1] > d50[-1]:
        score += 30
        notes.append("daily close above EMA50")
    if len(e50) > 7 and e50[-7] is not None and e50[-1] > e50[-7]:
        score += 20
        notes.append("4h EMA50 rising")
    return score, notes


def momentum_score(r: float | None) -> float:
    if r is None:
        return 50
    if 50 <= r <= 65:
        return 100
    if 40 <= r < 50:
        return 70
    if 65 < r <= 72:
        return 60
    if 30 <= r < 40:
        return 40
    return 20  # overbought (>72) or falling knife (<30)


def positioning_score(funding_rate: float | None) -> float:
    """Funding per 8h. Very positive = crowded longs (risk of a flush)."""
    if funding_rate is None:
        return 50
    if funding_rate <= -0.0001:
        return 75
    if funding_rate <= 0.0003:
        return 60
    if funding_rate <= 0.0008:
        return 40
    return 15


def risk_score(atr_pct: float | None) -> float:
    """Lower volatility = lower risk. 1.5% 4h ATR or less scores 100, 6%+ scores 0."""
    if atr_pct is None:
        return 50
    return max(0.0, min(100.0, 100 * (6 - atr_pct) / 4.5))


def label(score: float) -> str:
    if score >= 65:
        return "Bullish"
    if score >= 45:
        return "Neutral"
    return "Weak"


# ---------- per coin ----------

def analyze_coin(coin: str, ticker: dict, c4h: list, c1d: list, futures: dict,
                 sentiment: dict, fng: dict | None, prev_oi: float | None, cfg: dict) -> dict:
    risk_cfg = cfg["risk"]
    price = ticker["price"]
    closes = [c[4] for c in c4h]
    r = rsi(closes)
    a = atr(c4h)
    atr_pct = a / price * 100 if a and price else None
    e20 = ema(closes, 20)[-1] if closes else None

    t_score, t_notes = trend_score(c4h, c1d)
    comps = {
        "trend": t_score,
        "momentum": momentum_score(r),
        "sentiment": 50 + 50 * sentiment.get("adjusted", 0.0),
        "positioning": positioning_score(futures.get("funding_rate")),
        "risk": risk_score(atr_pct),
    }
    score = sum(WEIGHTS[k] * v for k, v in comps.items())
    if fng and fng["value"] >= 80:
        score -= 5  # euphoric market: late entries carry more risk

    oi = futures.get("open_interest")
    oi_change = (oi - prev_oi) / prev_oi * 100 if oi and prev_oi else None

    result = {
        "coin": coin,
        "price": price,
        "change_24h": ticker["change_24h"],
        "volume_24h": ticker["volume_24h"],
        "rsi": round(r, 1) if r is not None else None,
        "atr": a,
        "atr_pct": round(atr_pct, 2) if atr_pct is not None else None,
        "ema20": e20,
        "funding_rate": futures.get("funding_rate"),
        "open_interest": oi,
        "oi_change_pct": round(oi_change, 2) if oi_change is not None else None,
        "sentiment": sentiment,
        "components": {k: round(v, 1) for k, v in comps.items()},
        "score": round(score, 1),
        "label": label(score),
        "trend_notes": t_notes,
        "eligible": ticker["volume_24h"] >= risk_cfg["min_24h_volume"],
    }
    result["suggestion"] = suggest(result, risk_cfg)
    return result


def suggest(a: dict, risk_cfg: dict) -> dict | None:
    """A spot buy idea with entry, stop, targets and size, or None."""
    if not a["eligible"] or a["atr"] is None:
        return None
    if a["score"] < risk_cfg["min_score_to_suggest"] or a["components"]["trend"] < 55:
        return None
    if a["rsi"] is not None and a["rsi"] >= 72:
        return None

    price, atr_v = a["price"], a["atr"]
    stretched = a["ema20"] is not None and price - a["ema20"] > 1.5 * atr_v
    entry = a["ema20"] if stretched else price
    stop = entry - risk_cfg["atr_stop_multiplier"] * atr_v
    r = entry - stop
    if r <= 0:
        return None

    account = risk_cfg["account_size"]
    qty = account * risk_cfg["risk_per_trade_pct"] / 100 / r
    qty = min(qty, account * risk_cfg["max_position_pct"] / 100 / entry)

    reasons = [f"trend {a['components']['trend']:.0f}/100: " + ", ".join(a["trend_notes"])]
    s = a["sentiment"]
    if s.get("mentions"):
        reasons.append(f"social sentiment {s['score']:+.2f} across {s['mentions']} posts")
    else:
        reasons.append("no social mentions; based on price action only")
    if a["funding_rate"] is not None:
        reasons.append(f"futures funding {a['funding_rate'] * 100:.3f}% per 8h")
    if a["atr_pct"] is not None:
        reasons.append(f"4h volatility {a['atr_pct']:.1f}%")
    if stretched:
        reasons.append("price is stretched above its 20-period average, so wait for a pullback")

    return {
        "side": "BUY (spot)",
        "order": "limit (wait for pullback)" if stretched else "market",
        "entry": entry,
        "stop": stop,
        "tp1": entry + 1.5 * r,
        "tp2": entry + 3 * r,
        "qty": qty,
        "notional": qty * entry,
        "max_loss": qty * r,
        "confidence": "high" if a["score"] >= 75 else "medium",
        "reasons": reasons,
    }


def rank(analyses: list[dict], max_suggestions: int) -> list[dict]:
    ideas = [a for a in analyses if a["suggestion"]]
    ideas.sort(key=lambda a: a["score"], reverse=True)
    return ideas[:max_suggestions]


# ---------- track record ----------

def evaluate_open(history: list[dict], candles_by_coin: dict[str, list],
                  now: datetime | None = None, expiry_days: int = 14) -> list[dict]:
    """Settles open suggestions against price action since they were made.

    Conservative: if a candle touches both the stop and the target, the stop
    counts. Limit entries only count once price trades down to the entry.
    """
    now = now or datetime.now(timezone.utc)
    for h in history:
        if h["status"] not in ("open", "pending"):
            continue
        created_ms = datetime.fromisoformat(h["created"]).timestamp() * 1000
        for ts, _o, high, low, close, _v in (c[:6] for c in candles_by_coin.get(h["coin"], [])):
            if ts <= created_ms:
                continue
            if h["status"] == "pending":
                if low <= h["entry"]:
                    h["status"] = "open"
                else:
                    continue
            if low <= h["stop"]:
                h.update(status="stopped", result_r=-1.0, closed=_iso(ts))
                break
            if high >= h["tp1"]:
                h.update(status="target hit", result_r=1.5, closed=_iso(ts))
                break
            h["last_close"] = close
        created = datetime.fromisoformat(h["created"])
        if h["status"] in ("open", "pending") and now - created > timedelta(days=expiry_days):
            if h["status"] == "pending":
                h.update(status="never filled", result_r=0.0, closed=now.isoformat())
            else:
                last = h.get("last_close", h["entry"])
                h.update(status="expired", closed=now.isoformat(),
                         result_r=round((last - h["entry"]) / (h["entry"] - h["stop"]), 2))
    return history


def summarize_record(history: list[dict]) -> dict:
    closed = [h for h in history if "result_r" in h and h["status"] != "never filled"]
    wins = [h for h in closed if h["result_r"] > 0]
    return {
        "total": len(history),
        "open": sum(h["status"] in ("open", "pending") for h in history),
        "closed": len(closed),
        "win_rate": round(100 * len(wins) / len(closed)) if closed else None,
        "total_r": round(sum(h["result_r"] for h in closed), 2),
    }


def _iso(ts_ms: float) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()
