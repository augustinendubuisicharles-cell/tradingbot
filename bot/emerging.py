"""Emerging-token scanner: finds smaller AI and real-world-asset (RWA) tokens
that Bitget lists, rates their risk and potential, and plans entry and exit.

Discovery uses CoinGecko's free categories (no key). Timing is rules-based:
it waits for trend and momentum to confirm instead of guessing bottoms, and
every plan comes with a stop-loss, targets and an exit rule.
"""
import logging
import re
import time

import requests

from . import analysis
from .alerts import fmt_price

log = logging.getLogger(__name__)
CG = "https://api.coingecko.com/api/v3"
HTTP_TIMEOUT = 20
PAUSE = 4.0  # seconds between CoinGecko calls, to stay inside the keyless rate limit


def _get(path: str, **params):
    for attempt in range(3):
        r = requests.get(CG + path, params=params, timeout=HTTP_TIMEOUT,
                         headers={"User-Agent": "tradingbot/0.1"})
        if r.status_code == 429 and attempt < 2:  # free tier rate limit: back off and retry
            time.sleep(30)
            continue
        r.raise_for_status()
        time.sleep(PAUSE)
        return r.json()


# ---------- discovery ----------

def match_categories(categories: list[dict], narratives: dict[str, list[str]],
                     per_narrative: int = 3) -> dict[str, list[dict]]:
    """Picks the biggest CoinGecko categories whose names match each narrative."""
    out = {}
    for name, keywords in narratives.items():
        pats = [re.compile(rf"\b{re.escape(k)}\b", re.IGNORECASE) for k in keywords]
        hits = [c for c in categories if any(p.search(c.get("name", "")) for p in pats)]
        hits.sort(key=lambda c: c.get("market_cap") or 0, reverse=True)
        out[name] = hits[:per_narrative]
    return out


def discover(ecfg: dict) -> tuple[list[dict], dict[str, dict]]:
    """Candidate tokens (CoinGecko market rows tagged with a narrative) and
    narrative momentum (average 24h market-cap change of its categories)."""
    categories = _get("/coins/categories")
    matched = match_categories(categories, ecfg["narratives"])
    candidates: dict[str, dict] = {}
    momentum = {}
    for narrative, cats in matched.items():
        changes = [c.get("market_cap_change_24h") or 0.0 for c in cats]
        momentum[narrative] = {
            "change_24h": round(sum(changes) / len(changes), 2) if changes else None,
            "categories": [c["name"] for c in cats],
        }
        for cat in cats:
            rows = _get("/coins/markets", vs_currency="usd", category=cat["id"], order="volume_desc",
                        per_page=ecfg.get("per_category", 60), price_change_percentage="7d,30d")
            for row in rows:
                sym = (row.get("symbol") or "").upper()
                if sym and sym not in candidates:
                    candidates[sym] = {**row, "symbol": sym, "narrative": narrative}
    return list(candidates.values()), momentum


def shortlist(candidates: list[dict], listed: set[str], exclude: set[str], ecfg: dict) -> list[dict]:
    """Emerging = Bitget-listed, outside the main watchlist, small to mid size, liquid."""
    lo, hi = ecfg["min_market_cap"], ecfg["max_market_cap"]
    keep = [c for c in candidates
            if c["symbol"] in listed and c["symbol"] not in exclude
            and lo <= (c.get("market_cap") or 0) <= hi
            and (c.get("total_volume") or 0) >= ecfg["min_volume"]]
    keep.sort(key=lambda c: c.get("total_volume") or 0, reverse=True)
    return keep[:ecfg.get("max_tokens", 20)]


# ---------- risk and potential ----------

def risk_profile(cg: dict, atr_pct: float | None, bitget_volume: float) -> tuple[int, str, list[str]]:
    """0 (safest) .. 100 (riskiest), with a rating and the reasons."""
    score, notes = 0, []
    mcap = cg.get("market_cap") or 0
    if mcap < 50e6:
        score += 35; notes.append("very small market cap (under $50M)")
    elif mcap < 200e6:
        score += 25; notes.append("small market cap (under $200M)")
    elif mcap < 1e9:
        score += 15; notes.append("mid market cap")
    else:
        score += 8
    if bitget_volume < 2e6:
        score += 25; notes.append("thin Bitget volume (under $2M a day)")
    elif bitget_volume < 10e6:
        score += 12; notes.append("moderate Bitget volume")
    else:
        score += 4
    if atr_pct is not None:
        if atr_pct > 8:
            score += 25; notes.append(f"very volatile ({atr_pct:.1f}% per 4h candle)")
        elif atr_pct > 5:
            score += 15; notes.append(f"volatile ({atr_pct:.1f}% per 4h candle)")
        else:
            score += 5
    circ, total = cg.get("circulating_supply") or 0, cg.get("total_supply") or cg.get("max_supply") or 0
    if total and circ / total < 0.5:
        score += 10; notes.append(f"only {100 * circ / total:.0f}% of supply unlocked (future selling pressure)")
    if (cg.get("ath_change_percentage") or 0) < -90:
        score += 5; notes.append("over 90% below its all-time high")
    score = min(score, 100)
    rating = "Low" if score < 35 else "Medium" if score < 55 else "High" if score < 75 else "Very high"
    return score, rating, notes


def volume_surge(c4h: list[list[float]]) -> float | None:
    """Last 24h of 4h volume vs the previous 5 days' average (1.0 = normal)."""
    if len(c4h) < 36:
        return None
    recent = sum(c[5] for c in c4h[-6:]) / 6
    base = sum(c[5] for c in c4h[-36:-6]) / 30
    return recent / base if base else None


def potential_score(cg: dict, trend: float, surge: float | None, narrative_change: float | None,
                    mentions: int, trending: bool) -> tuple[float, list[str]]:
    score, notes = 0.0, []
    score += 0.35 * trend
    ch7 = cg.get("price_change_percentage_7d_in_currency") or 0.0
    ch30 = cg.get("price_change_percentage_30d_in_currency") or 0.0
    rel = max(-30.0, min(30.0, 0.6 * ch7 + 0.4 * ch30))
    score += 0.25 * (50 + rel * 50 / 30)
    if ch7 > 0:
        notes.append(f"up {ch7:.0f}% this week")
    if surge is not None:
        score += 0.15 * max(0.0, min(100.0, (surge - 0.5) * 66))
        if surge >= 1.5:
            notes.append(f"volume {surge:.1f}× normal")
    if narrative_change is not None:
        score += 0.15 * max(0.0, min(100.0, 50 + narrative_change * 5))
        if narrative_change > 2:
            notes.append(f"narrative gaining ({narrative_change:+.1f}% today)")
    buzz = min(100.0, mentions * 25 + (50 if trending else 0))
    score += 0.10 * buzz
    if trending:
        notes.append("trending on CoinGecko")
    if mentions:
        notes.append(f"mentioned in {mentions} trader/news posts")
    return round(score, 1), notes


# ---------- timing ----------

RISK_PCT = {"Low": 1.0, "Medium": 0.75, "High": 0.5, "Very high": 0.25}


def timing_plan(c4h: list[list[float]], c1d: list[list[float]], trend: float, rating: str,
                risk_cfg: dict, max_position_pct: float) -> dict:
    """When to get in, where the stop goes, and when to get out."""
    closes = [c[4] for c in c4h]
    price = closes[-1]
    e20 = analysis.ema(closes, 20)[-1]
    e50 = analysis.ema(closes, 50)[-1]
    r = analysis.rsi(closes)
    a = analysis.atr(c4h)
    d_closes = [c[4] for c in c1d]
    d50 = analysis.ema(d_closes, 50)[-1] if len(d_closes) >= 50 else None
    high20 = max(c[2] for c in c4h[-21:-1])
    swing_low = min(c[3] for c in c4h[-10:])
    plan = {"price": price, "rsi": round(r, 1) if r is not None else None}

    if a is None or e20 is None or e50 is None:
        return {**plan, "status": "Not enough history", "action": "Too new to judge; check back later."}

    downtrend = price < e50 and (d50 is None or d_closes[-1] < d50)
    if downtrend and trend < 30:
        return {**plan, "status": "Avoid",
                "action": "In a downtrend. Wait until a 4h candle closes above "
                          f"{fmt_price(e50)} (its 50-period average) before considering it."}
    prev_r = analysis.rsi(closes[:-1])
    if r is not None and r >= 78:
        return {**plan, "status": "Take profit",
                "action": f"Overheated (RSI {r:.0f}). If you hold it, sell some now. "
                          f"Don't buy; wait for a pullback towards {fmt_price(e20)}."}
    if e20 > e50 and price < e20 and r is not None and prev_r is not None and r < 50 and r < prev_r:
        return {**plan, "status": "Exit signal",
                "action": f"Uptrend is breaking: price closed below {fmt_price(e20)} (20-period average) "
                          "and momentum is fading. If you hold it, this is the time to leave."}

    if trend >= 55 and r is not None and r <= 70 and price - e20 <= 1.0 * a:
        status, entry = "Enter zone", price
        action = "Uptrend confirmed and not overextended. A good time to position, at today's price."
    elif trend >= 55:
        status, entry = "Wait for pullback", e20
        action = (f"Uptrend, but price is stretched. Set a buy around {fmt_price(e20)} "
                  "(its 20-period average) instead of chasing.")
    else:
        status, entry = "Watch for breakout", high20 * 1.005
        action = (f"Not trending yet. Buy only if a 4h candle closes above {fmt_price(high20)} "
                  "(the recent high) on rising volume.")

    stop = min(swing_low, entry - 2.5 * a) if status == "Enter zone" else entry - 2.5 * a
    risk_per_unit = entry - stop
    if risk_per_unit <= 0:
        return {**plan, "status": "Avoid", "action": "No sensible stop-loss level right now."}
    account = risk_cfg["account_size"]
    pct = RISK_PCT[rating]
    qty = min(account * pct / 100 / risk_per_unit, account * max_position_pct / 100 / entry)
    return {
        **plan,
        "status": status,
        "action": action,
        "entry": entry,
        "stop": stop,
        "tp1": entry + 2 * risk_per_unit,
        "tp2": entry + 4 * risk_per_unit,
        "risk_pct": pct,
        "notional": qty * entry,
        "max_loss": qty * risk_per_unit,
        "exit_rule": (f"Sell half at target 1 and move the stop to {fmt_price(entry)} (breakeven). "
                      "Exit the rest at target 2, on a 4h close below the 20-period average, "
                      "or if RSI tops 80 and turns down. Leave after 10 days if it hasn't moved."),
    }


# ---------- whole scan ----------

STATUS_ORDER = {"Enter zone": 0, "Wait for pullback": 1, "Watch for breakout": 2,
                "Take profit": 3, "Exit signal": 4, "Not enough history": 5, "Avoid": 6}


def mention_pattern(sym: str, name: str) -> re.Pattern:
    """$SYM, #SYM, SYM/USDT, or the full name when it's distinctive (6+ letters)."""
    alts = [rf"[$#]{re.escape(sym)}\b", rf"\b{re.escape(sym)}/?USDT\b"]
    if len(name) >= 6:
        alts.append(rf"\b{re.escape(name)}\b")
    return re.compile("|".join(alts), re.IGNORECASE)


def rate(picks: list[dict], momentum: dict, mkt, cfg: dict, posts_text: list[str],
         trending: list[str]) -> list[dict]:
    ecfg = cfg["emerging"]
    tickers = mkt.tickers([p["symbol"] for p in picks])
    out = []
    for cg in picks:
        sym = cg["symbol"]
        t = tickers.get(sym)
        if not t:
            continue
        cg_price = cg.get("current_price") or 0
        if cg_price and abs(t["price"] / cg_price - 1) > 0.15:
            # Same ticker, different coin on Bitget: skip rather than mix them up.
            log.info("skipping %s: Bitget price %.6g vs CoinGecko %.6g", sym, t["price"], cg_price)
            continue
        try:
            c4h = mkt.candles(sym, "4h", 200)
            c1d = mkt.candles(sym, "1d", 120)
        except Exception as e:  # noqa: BLE001
            log.warning("candles unavailable for %s: %s", sym, e)
            continue
        if len(c4h) < 60:
            continue
        price = c4h[-1][4]
        a = analysis.atr(c4h)
        atr_pct = a / price * 100 if a and price else None
        trend, _ = analysis.trend_score(c4h, c1d)
        risk, rating, risk_notes = risk_profile(cg, atr_pct, t["volume_24h"])
        pat = mention_pattern(sym, cg.get("name", ""))
        mentions = sum(1 for text in posts_text if pat.search(text))
        pot, pot_notes = potential_score(cg, trend, volume_surge(c4h),
                                         momentum.get(cg["narrative"], {}).get("change_24h"),
                                         mentions, sym in set(trending))
        plan = timing_plan(c4h, c1d, trend, rating, cfg["risk"], ecfg.get("max_position_pct", 10))
        out.append({
            "symbol": sym, "name": cg.get("name", sym), "narrative": cg["narrative"],
            "market_cap": cg.get("market_cap"), "change_7d": cg.get("price_change_percentage_7d_in_currency"),
            "change_30d": cg.get("price_change_percentage_30d_in_currency"),
            "bitget_volume": t["volume_24h"], "trend": trend,
            "risk_score": risk, "risk_rating": rating, "risk_notes": risk_notes,
            "potential": pot, "potential_notes": pot_notes, "plan": plan,
        })
    out.sort(key=lambda x: (STATUS_ORDER.get(x["plan"]["status"], 9), -x["potential"]))
    return out


def scan(cfg: dict, mkt, posts_text: list[str], trending: list[str]) -> tuple[list[dict], dict, str]:
    """Rated tokens (best first), narrative momentum, and a status line."""
    ecfg = cfg["emerging"]
    candidates, momentum = discover(ecfg)
    mkt.spot.load_markets()
    listed = {m.split("/")[0] for m in mkt.spot.markets if m.endswith(f"/{mkt.quote}")}
    picks = shortlist(candidates, listed, set(cfg["watchlist"]), ecfg)
    out = rate(picks, momentum, mkt, cfg, posts_text, trending)
    return out, momentum, f"ok ({len(candidates)} AI/RWA tokens found, {len(out)} on Bitget rated)"


def status_alerts(tokens: list[dict], state: dict) -> list[str]:
    """Telegram messages for tokens that just moved into an entry or exit state."""
    prev = state.get("emerging_status", {})
    msgs = []
    for t in tokens:
        st, p = t["plan"]["status"], t["plan"]
        if st == prev.get(t["symbol"]):
            continue
        head = f"<b>{t['symbol']}</b> ({t['narrative']}, {t['risk_rating'].lower()} risk)"
        if st == "Enter zone":
            msgs.append(f"🌱 <b>Emerging token: time to position</b>\n{head}\n"
                        f"Entry {fmt_price(p['entry'])} · stop {fmt_price(p['stop'])} · "
                        f"targets {fmt_price(p['tp1'])} / {fmt_price(p['tp2'])}\n"
                        f"Size ≈ {p['notional']:,.0f} USDT (max loss {p['max_loss']:,.0f})\n{p['exit_rule']}")
        elif st in ("Exit signal", "Take profit") and prev.get(t["symbol"]) in ("Enter zone", "Wait for pullback"):
            msgs.append(f"🚪 <b>Emerging token: {st.lower()}</b>\n{head}\n{p['action']}")
    state["emerging_status"] = {t["symbol"]: t["plan"]["status"] for t in tokens}
    return msgs
