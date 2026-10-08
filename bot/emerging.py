"""Hidden-gem scanner: small, newer tokens from every crypto narrative that
Bitget lists, put through safety checks, scored for growth potential, and
given an entry, a stop, targets and an exit plan.

Every morning a full scan picks the candidates (CoinGecko, GoPlus contract
scanner, Bitget; all free, no keys). The 4-hourly runs re-check the timing of
those picks with fresh Bitget prices and alert when one moves into a buy or
an exit.
"""
import logging
import re
import time
from datetime import datetime, timezone

import requests

from . import analysis
from .alerts import fmt_price

log = logging.getLogger(__name__)
CG = "https://api.coingecko.com/api/v3"
GOPLUS = "https://api.gopluslabs.io/api/v1/token_security/{chain}"
HTTP_TIMEOUT = 20
PAUSE = 6.0  # seconds between CoinGecko calls, to stay inside the keyless rate limit

# Stablecoins, wrapped/staked copies and tokenised gold or stocks aren't "gems".
SKIP_NAME = re.compile(r"\b(usd|eur|gbp|wrapped|bridged|staked|restaked|liquid staking|tokenized|"
                       r"xstock|gold|peg|bitcoin|ether)\b|^(w|st|ws|cb|ren)?(btc|eth)$|usd", re.IGNORECASE)
# CoinGecko tags that describe a chain, investor or listing rather than a narrative.
GENERIC_CAT = re.compile(r"ecosystem|portfolio|launch|hodler|alpha|made in|alleged|airdrop|listing|"
                         r"holdings|binance|coinbase|bitget|upbit|robinhood|index|tokens? unlock|"
                         r"proof of|fan token|world liberty|ventures|capital|labs|stake|delisted",
                         re.IGNORECASE)
# CoinGecko platform id -> GoPlus chain id (EVM chains the free scanner covers).
CHAINS = {"ethereum": "1", "binance-smart-chain": "56", "base": "8453", "arbitrum-one": "42161",
          "polygon-pos": "137", "avalanche": "43114", "optimistic-ethereum": "10",
          "linea": "59144", "zksync": "324", "mantle": "5000", "blast": "81457"}


def _get(path: str, **params):
    for attempt in range(3):
        r = requests.get(CG + path, params=params, timeout=HTTP_TIMEOUT,
                         headers={"User-Agent": "tradingbot/0.1"})
        if r.status_code == 429 and attempt < 2:  # free tier rate limit: back off and retry
            time.sleep(45)
            continue
        r.raise_for_status()
        time.sleep(PAUSE)
        return r.json()


# ---------- discovery (morning) ----------

def universe(ecfg: dict) -> list[dict]:
    """The most-traded coins on CoinGecko (all narratives), with 7d and 30d change."""
    rows = []
    for page in range(1, ecfg.get("universe_pages", 4) + 1):
        rows += _get("/coins/markets", vs_currency="usd", order="volume_desc", per_page=250, page=page,
                     price_change_percentage="24h,7d,30d")
    return rows


def prefilter(rows: list[dict], listed: set[str], exclude: set[str], ecfg: dict) -> list[dict]:
    """Small, liquid, Bitget-listed, not a stablecoin or wrapped copy, not mid-pump.
    Ranked by turnover (volume vs size), an early sign that attention is arriving."""
    seen, keep = set(), []
    for c in rows:
        sym = (c.get("symbol") or "").upper()
        mcap, vol = c.get("market_cap") or 0, c.get("total_volume") or 0
        if (not sym or sym in seen or sym not in listed or sym in exclude
                or SKIP_NAME.search(c.get("name", "")) or SKIP_NAME.search(sym)
                or not ecfg["min_market_cap"] <= mcap <= ecfg["max_market_cap"]
                or vol < ecfg["min_volume"]
                or (c.get("price_change_percentage_7d_in_currency") or 0) > ecfg["max_7d_gain"]
                or (c.get("ath_change_percentage") or 0) < -95):
            continue
        seen.add(sym)
        keep.append({**c, "symbol": sym})
    keep.sort(key=lambda c: (c["total_volume"] / c["market_cap"])
              + max(0.0, c.get("price_change_percentage_7d_in_currency") or 0) / 100, reverse=True)
    return keep[:ecfg.get("max_details", 20)]


def narrative_of(categories: list[str]) -> str:
    useful = [c for c in categories or [] if c and not GENERIC_CAT.search(c)]
    return useful[0] if useful else "Other"


def contract_check(platforms: dict | None) -> tuple[bool | None, str, list[str]]:
    """GoPlus token-security scan. (passed, summary, warnings); passed None = couldn't check."""
    platforms = {k: v for k, v in (platforms or {}).items() if k and v}
    if not platforms:
        return True, "native coin of its own blockchain (no token contract to exploit)", []
    chain_key = next((k for k in platforms if k in CHAINS), None)
    if chain_key is None:
        return None, f"contract on {next(iter(platforms))}, which the free scanner doesn't cover", []
    addr = platforms[chain_key].lower()
    try:
        r = requests.get(GOPLUS.format(chain=CHAINS[chain_key]), params={"contract_addresses": addr},
                         timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        info = (r.json().get("result") or {}).get(addr)
        time.sleep(2)
    except Exception as e:  # noqa: BLE001
        log.warning("goplus check failed for %s: %s", addr, e)
        return None, "contract scanner unavailable", []
    if not info:
        return None, "contract not in the scanner's database yet", []
    flag = lambda k: str(info.get(k, "0")) == "1"  # noqa: E731
    tax = lambda k: float(info.get(k) or 0)  # noqa: E731
    fails = []
    if flag("is_honeypot"): fails.append("honeypot (can't sell)")
    if flag("cannot_sell_all"): fails.append("blocks selling your full balance")
    if flag("owner_change_balance"): fails.append("owner can change balances")
    if flag("hidden_owner"): fails.append("hidden owner")
    if flag("selfdestruct"): fails.append("can self-destruct")
    if str(info.get("is_open_source", "1")) == "0": fails.append("code not public")
    if tax("buy_tax") > 0.05 or tax("sell_tax") > 0.05:
        fails.append(f"high tax (buy {tax('buy_tax'):.0%}, sell {tax('sell_tax'):.0%})")
    warns = []
    if flag("is_mintable"): warns.append("team can mint more tokens")
    if flag("transfer_pausable"): warns.append("transfers can be paused")
    if flag("is_proxy"): warns.append("upgradeable contract")
    if fails:
        return False, "contract scan failed: " + ", ".join(fails), warns
    return True, f"contract scanned clean on {chain_key.replace('-', ' ')}", warns


def details(cg_id: str) -> dict:
    d = _get(f"/coins/{cg_id}", localization="false", tickers="false", market_data="false",
             community_data="false", developer_data="true", sparkline="false")
    return {
        "categories": d.get("categories") or [],
        "platforms": d.get("platforms") or {},
        "homepage": next((u for u in (d.get("links") or {}).get("homepage", []) if u), ""),
        "watchers": d.get("watchlist_portfolio_users"),
        "commits_4w": ((d.get("developer_data") or {}).get("commit_count_4_weeks")),
    }


# ---------- safety, risk and potential ----------

def safety_checks(cg: dict, base: dict, bitget_volume: float, atr_pct: float | None,
                  ecfg: dict) -> list[dict]:
    """Each check: name, ok (True / False / None = unknown), detail."""
    mcap = cg.get("market_cap") or 0
    turnover = (cg.get("total_volume") or 0) / mcap if mcap else 0
    circ = cg.get("circulating_supply") or 0
    supply = cg.get("max_supply") or cg.get("total_supply") or 0
    fdv = cg.get("fully_diluted_valuation") or 0
    ch7 = cg.get("price_change_percentage_7d_in_currency") or 0
    ch24 = cg.get("price_change_percentage_24h_in_currency") or cg.get("price_change_percentage_24h") or 0
    age = base.get("age_days")
    checks = [
        ("Real trading volume",
         bitget_volume >= ecfg["min_bitget_volume"] and 0.02 <= turnover <= 1.5,
         f"${bitget_volume / 1e6:.1f}M a day on Bitget, {turnover:.0%} of its value traded daily"),
        ("Most supply unlocked",
         (circ / supply >= ecfg["min_unlocked"]) if supply else None,
         f"{100 * circ / supply:.0f}% of supply in circulation" if supply else "supply data missing"),
        ("Fair valuation",
         (fdv <= 4 * mcap) if fdv and mcap else None,
         f"fully diluted value {fdv / mcap:.1f}× market cap" if fdv and mcap else "no diluted value data"),
        ("Contract safe", base.get("contract_ok"), base.get("contract_note", "not checked")),
        ("Not a pump-and-dump", ch7 <= ecfg["max_7d_gain"] and ch24 <= 60,
         f"{ch24:+.0f}% today, {ch7:+.0f}% this week"),
        ("Enough trading history", (age >= ecfg["min_age_days"]) if age is not None else None,
         f"trading on Bitget for {age} days" + ("+" if base.get("age_capped") else "") if age is not None
         else "listing date unknown"),
        ("Volatility under control", (atr_pct <= 12) if atr_pct is not None else None,
         f"moves {atr_pct:.1f}% per 4h candle on average" if atr_pct is not None else "not enough data"),
        ("Real project", bool(base.get("homepage")) or None,
         ("has a website" if base.get("homepage") else "no website listed")
         + (f", {base['commits_4w']} code updates in 4 weeks" if base.get("commits_4w") else "")),
    ]
    return [{"name": n, "ok": ok, "detail": d} for n, ok, d in checks]


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

    # Stop under the recent swing low, but never closer than 1 or further than 2.5 average 4h ranges.
    stop = max(min(swing_low, entry - a), entry - 2.5 * a) if status == "Enter zone" else entry - 2.5 * a
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



def mention_pattern(sym: str, name: str) -> re.Pattern:
    """$SYM, #SYM, SYM/USDT, or the full name when it's distinctive (6+ letters)."""
    alts = [rf"[$#]{re.escape(sym)}\b", rf"\b{re.escape(sym)}/?USDT\b"]
    if len(name) >= 6:
        alts.append(rf"\b{re.escape(name)}\b")
    return re.compile("|".join(alts), re.IGNORECASE)




def potential_score(cg: dict, base: dict, trend: float, surge: float | None, mentions: int,
                    trending: bool, ecfg: dict) -> tuple[float, list[str]]:
    """0..100: how much room and fuel the token has, and how early it still is."""
    notes = []
    score = 0.25 * trend
    ch7 = cg.get("price_change_percentage_7d_in_currency") or 0.0
    ch30 = cg.get("price_change_percentage_30d_in_currency") or 0.0
    rel = max(-30.0, min(30.0, 0.6 * ch7 + 0.4 * ch30))
    score += 0.15 * (50 + rel * 50 / 30)
    if ch7 >= 5:
        notes.append(f"Up {ch7:.0f}% this week")
    if surge is not None:
        score += 0.10 * max(0.0, min(100.0, (surge - 0.5) * 66))
        if surge >= 1.5:
            notes.append(f"Trading volume is {surge:.1f}× its normal level, so buyers are arriving")
    nch = base.get("narrative_change")
    if nch is not None:
        score += 0.15 * max(0.0, min(100.0, 50 + nch * 5))
        if nch > 2:
            notes.append(f"Its sector ({base['narrative']}) is rising today ({nch:+.1f}%)")
    # Early: few people watching it yet, not trending, rarely mentioned.
    watchers = base.get("watchers")
    early = 50.0 if watchers is None else max(0.0, 100 - watchers / 500)
    if trending:
        early -= 30
    if mentions > 3:
        early -= 20
    score += 0.20 * max(0.0, early)
    if watchers is not None and watchers < 20000:
        notes.append(f"Still under the radar: only {watchers:,} people watch it on CoinGecko")
    if mentions:
        notes.append(f"Traders are starting to talk about it ({mentions} posts)")
    mcap = cg.get("market_cap") or ecfg["max_market_cap"]
    room = max(0.0, min(100.0, 100 * (ecfg["max_market_cap"] - mcap) / (ecfg["max_market_cap"] - 30e6)))
    score += 0.10 * room
    age = base.get("age_days")
    if age is not None and not base.get("age_capped"):
        score += 0.05 * (100 if age <= 60 else 60 if age <= 180 else 20)
        if age <= 180:
            notes.append(f"New listing: on Bitget for only {age} days")
    else:
        score += 0.05 * 20
    return round(score, 1), notes


def scenarios(cg: dict, plan: dict) -> list[dict]:
    """Bear / base / stretch / bull outcomes from the entry, with the market cap each implies."""
    if "entry" not in plan:
        return []
    entry, price, mcap = plan["entry"], plan["price"], cg.get("market_cap") or 0
    rows = [("Bear", plan["stop"], "stop-loss hit"),
            ("Base", plan["tp1"], "first target, if the setup works"),
            ("Stretch", plan["tp2"], "second target, if the trend runs")]
    ath = cg.get("ath") or 0
    if ath > plan["tp2"] and ath <= entry * 6:
        rows.append(("Bull", ath, "back to its all-time high"))
    return [{"case": c, "price": p, "pct": (p / entry - 1) * 100,
             "mcap": mcap * p / price if price else None, "note": n} for c, p, n in rows]


# ---------- rating a token ----------

def build_token(base: dict, c4h: list, c1d: list, bitget_volume: float, cfg: dict,
                posts_text: list[str], trending: set[str]) -> dict:
    ecfg, cg, sym = cfg["emerging"], base["cg"], base["symbol"]
    price = c4h[-1][4]
    a = analysis.atr(c4h)
    atr_pct = a / price * 100 if a and price else None
    trend, _ = analysis.trend_score(c4h, c1d)
    checks = safety_checks(cg, base, bitget_volume, atr_pct, ecfg)
    risk, rating, risk_notes = risk_profile(cg, atr_pct, bitget_volume)
    risk_notes += base.get("contract_warnings", [])
    pat = mention_pattern(sym, cg.get("name", ""))
    mentions = sum(1 for text in posts_text if pat.search(text))
    pot, pot_notes = potential_score(cg, base, trend, volume_surge(c4h), mentions, sym in trending, ecfg)
    plan = timing_plan(c4h, c1d, trend, rating, cfg["risk"], ecfg.get("max_position_pct", 10))
    failed = [c["name"] for c in checks if c["ok"] is False]
    return {
        "symbol": sym, "name": cg.get("name", sym), "narrative": base["narrative"],
        "market_cap": cg.get("market_cap"), "change_7d": cg.get("price_change_percentage_7d_in_currency"),
        "bitget_volume": bitget_volume, "trend": trend, "age_days": base.get("age_days"),
        "safety": checks, "safe": not failed, "failed_checks": failed,
        "unknown_checks": [c["name"] for c in checks if c["ok"] is None],
        "risk_score": risk, "risk_rating": rating, "risk_notes": risk_notes,
        "potential": pot, "potential_notes": pot_notes, "plan": plan,
        "scenarios": scenarios(cg, plan),
    }


BUY_STATES = ("Enter zone", "Wait for pullback", "Watch for breakout")


def order(tokens: list[dict]) -> list[dict]:
    """Safe first, then ones with a buy setup, then fully verified, then by growth potential."""
    return sorted(tokens, key=lambda t: (not t["safe"], t["plan"]["status"] not in BUY_STATES,
                                         len(t["unknown_checks"]) > 1, -t["potential"]))


def _bitget_price_matches(cg: dict, ticker: dict) -> bool:
    p = cg.get("current_price") or 0
    return not p or abs(ticker["price"] / p - 1) <= 0.15


def _rate_bases(bases: list[dict], mkt, cfg: dict, posts_text: list[str], trending: list[str]) -> list[dict]:
    tickers = mkt.tickers([b["symbol"] for b in bases])
    out = []
    for b in bases:
        t = tickers.get(b["symbol"])
        if not t:
            continue
        try:
            c4h = mkt.candles(b["symbol"], "4h", 200)
            c1d = mkt.candles(b["symbol"], "1d", 120)
        except Exception as e:  # noqa: BLE001
            log.warning("candles unavailable for %s: %s", b["symbol"], e)
            continue
        if len(c4h) < 60:
            continue
        out.append(build_token(b, c4h, c1d, t["volume_24h"], cfg, posts_text, set(trending)))
    return order(out)


def morning_scan(cfg: dict, mkt, posts_text: list[str], trending: list[str]) -> tuple[list[dict], dict]:
    """Full discovery. Returns rated tokens and the saved scan (bases + hottest narratives)."""
    ecfg = cfg["emerging"]
    rows = universe(ecfg)
    mkt.spot.load_markets()
    listed = {m.split("/")[0] for m in mkt.spot.markets if m.endswith(f"/{mkt.quote}")}
    cands = prefilter(rows, listed, set(cfg["watchlist"]), ecfg)
    tickers = mkt.tickers([c["symbol"] for c in cands])
    cats = _get("/coins/categories")
    cat_change = {c["name"]: c.get("market_cap_change_24h") for c in cats}
    hot = sorted((c for c in cats if (c.get("market_cap") or 0) > 2e8 and not GENERIC_CAT.search(c["name"])
                  and c.get("market_cap_change_24h") is not None),
                 key=lambda c: c["market_cap_change_24h"], reverse=True)[:6]
    bases = []
    for cg in cands:
        t = tickers.get(cg["symbol"])
        if not t or not _bitget_price_matches(cg, t):
            continue
        try:
            det = details(cg["id"])
            daily = mkt.candles(cg["symbol"], "1d", 300)  # Bitget returns at most 300 days
        except Exception as e:  # noqa: BLE001
            log.warning("details unavailable for %s: %s", cg["symbol"], e)
            continue
        narrative = narrative_of(det["categories"])
        ok, note, warns = contract_check(det["platforms"])
        bases.append({
            "symbol": cg["symbol"], "cg": cg, "narrative": narrative,
            "narrative_change": cat_change.get(narrative),
            "watchers": det["watchers"], "homepage": det["homepage"], "commits_4w": det["commits_4w"],
            "age_days": len(daily), "age_capped": len(daily) >= 300,
            "contract_ok": ok, "contract_note": note, "contract_warnings": warns,
        })
    scan = {"as_of": datetime.now(timezone.utc).isoformat(), "bases": bases,
            "hot_narratives": [{"name": c["name"], "change_24h": round(c["market_cap_change_24h"], 1)}
                               for c in hot]}
    return _rate_bases(bases, mkt, cfg, posts_text, trending), scan


def rerate(scan: dict, mkt, cfg: dict, posts_text: list[str], trending: list[str]) -> list[dict]:
    """4-hourly: fresh Bitget prices and timing for this morning's picks (no CoinGecko calls)."""
    return _rate_bases(scan.get("bases", []), mkt, cfg, posts_text, trending)


# ---------- messages ----------

def status_alerts(tokens: list[dict], state: dict) -> list[str]:
    """Telegram messages for safe tokens that just moved into a buy or an exit state."""
    prev = state.get("emerging_status", {})
    msgs = []
    for t in tokens:
        st, p = t["plan"]["status"], t["plan"]
        if not t["safe"] or st == prev.get(t["symbol"]):
            continue
        head = f"<b>{t['symbol']}</b> ({t['narrative']}, {t['risk_rating'].lower()} risk)"
        if st == "Enter zone":
            msgs.append(f"🌱 <b>Hidden gem: time to position</b>\n{head}\n"
                        f"Entry {fmt_price(p['entry'])} · stop {fmt_price(p['stop'])} · "
                        f"targets {fmt_price(p['tp1'])} / {fmt_price(p['tp2'])}\n"
                        f"Size ≈ {p['notional']:,.0f} USDT (max loss {p['max_loss']:,.0f})\n{p['exit_rule']}")
        elif st in ("Exit signal", "Take profit") and prev.get(t["symbol"]) in ("Enter zone", "Wait for pullback"):
            msgs.append(f"🚪 <b>Hidden gem: {st.lower()}</b>\n{head}\n{p['action']}")
    state["emerging_status"] = {t["symbol"]: t["plan"]["status"] for t in tokens}
    return msgs


def _mcap(v: float | None) -> str:
    if not v:
        return "?"
    return f"${v / 1e9:.1f}B" if v >= 1e9 else f"${v / 1e6:.0f}M"


RISK_WORDS = {"Low": "Low", "Medium": "Medium", "High": "High (big swings)",
              "Very high": "Very high (big swings, small size)"}


def _plan_line(t: dict) -> str:
    p = t["plan"]
    if p["status"] == "Enter zone":
        return f"🟢 <b>BUY NOW</b> at about {fmt_price(p['entry'])}"
    if p["status"] == "Wait for pullback":
        return (f"🟡 <b>WAIT, then buy at {fmt_price(p['entry'])}</b>\n"
                f"It's running hot at {fmt_price(p['price'])}. Set a buy order lower; don't chase it.")
    return (f"🟡 <b>WAIT for a breakout above {fmt_price(p['entry'])}</b>\n"
            f"Now {fmt_price(p['price'])}. Buy only if a 4-hour candle closes above that level.")


def _pick_message(i: int, t: dict) -> str:
    import html
    esc = lambda x: html.escape(str(x), quote=False)  # noqa: E731
    p = t["plan"]
    passed, total = sum(1 for c in t["safety"] if c["ok"]), len(t["safety"])
    nums = "1️⃣ 2️⃣ 3️⃣ 4️⃣ 5️⃣ 6️⃣ 7️⃣ 8️⃣ 9️⃣".split()
    lines = [f"{nums[i - 1] if i <= 9 else str(i) + '.'} <b>{t['symbol']}</b> ({esc(t['name'])})",
             f"{esc(t['narrative'])} · market cap {_mcap(t['market_cap'])}",
             "",
             _plan_line(t),
             "",
             f"📈 Growth score: <b>{t['potential']:.0f}/100</b>",
             f"⚠️ Risk: <b>{RISK_WORDS[t['risk_rating']]}</b>",
             f"🛡 Safety: passed <b>{passed} of {total}</b> checks"
             + (f" ({esc(', '.join(t['unknown_checks']).lower())} couldn't be verified)" if t["unknown_checks"] else "")]
    if t["potential_notes"]:
        lines += ["", "<b>Why it could grow</b>"] + [f"• {esc(n)}" for n in t["potential_notes"][:4]]
    if t["scenarios"]:
        sc = {s["case"]: s for s in t["scenarios"]}
        lines += ["", "<b>The plan</b>",
                  f"• Buy: {fmt_price(p['entry'])} with about {p['notional']:,.0f} USDT",
                  f"• Stop-loss: {fmt_price(p['stop'])} ({sc['Bear']['pct']:+.0f}%). If it falls here, sell. "
                  f"You'd lose about {p['max_loss']:,.0f} USDT.",
                  f"• Target 1: {fmt_price(p['tp1'])} ({sc['Base']['pct']:+.0f}%). Sell half, move your stop to the buy price.",
                  f"• Target 2: {fmt_price(p['tp2'])} ({sc['Stretch']['pct']:+.0f}%). Sell the rest."]
        if "Bull" in sc:
            lines.append(f"• Best case: {fmt_price(sc['Bull']['price'])} ({sc['Bull']['pct']:+.0f}%) "
                         "if it returns to its all-time high")
    return "\n".join(lines)


def morning_messages(tokens: list[dict], hot: list[dict], n: int, dashboard_url: str | None) -> list[str]:
    """The morning report as separate Telegram messages: intro, one per pick, then a guide."""
    import html
    today = datetime.now(timezone.utc).strftime("%A %d %B")
    picks = [t for t in tokens if t["safe"] and t["plan"]["status"] in BUY_STATES][:n]
    rejected = [t for t in tokens if not t["safe"]]
    intro = [f"🌅 <b>Morning gems</b> · {today}", ""]
    if picks:
        intro.append(f"I checked {len(tokens)} small, little-known tokens from every sector. "
                     f"<b>{len(picks)}</b> passed the safety checks and have a buy setup. Best first:")
    else:
        intro.append(f"I checked {len(tokens)} small, little-known tokens from every sector. None passed "
                     "every safety check with a buy setup today, so the best move is to wait.")
    if hot:
        intro += ["", "🔥 <b>Sectors rising today</b>"] + [
            f"• {html.escape(h['name'], quote=False)} {h['change_24h']:+.1f}%" for h in hot[:4]]
    msgs = ["\n".join(intro)] + [_pick_message(i, t) for i, t in enumerate(picks, 1)]
    guide = ["📘 <b>How to read this</b>",
             "• <b>Growth score</b>: how much room and momentum a token has. Higher is better.",
             "• <b>Risk</b>: how wildly the price swings. Riskier tokens get smaller amounts, "
             "so a stop-loss never costs more than 0.25–1% of your account.",
             "• <b>Stop-loss</b>: the price where you sell to keep a loss small.",
             "• <b>Leave early</b> if a 4-hour candle closes below its trend line, or after 10 days with no move. "
             "I'll message you when that happens."]
    if rejected:
        reasons: dict[str, int] = {}
        for t in rejected:
            for c in t["failed_checks"]:
                reasons[c] = reasons.get(c, 0) + 1
        top = max(reasons, key=reasons.get)
        why = {"Most supply unlocked": "too many tokens still locked, so more selling is coming",
               "Fair valuation": "too many tokens still to be released",
               "Real trading volume": "too little trading to get in and out safely",
               "Contract safe": "risky contract code",
               "Not a pump-and-dump": "already pumped too hard",
               "Volatility under control": "price swings too wild",
               "Enough trading history": "too new to judge"}.get(top, top.lower())
        guide += ["", f"🚫 <b>Rejected today:</b> {', '.join(t['symbol'] for t in rejected)}. "
                      f"The most common reason was {why}."]
    if dashboard_url:
        guide.append(f'\n<a href="{html.escape(dashboard_url)}">Open the dashboard for full details</a>')
    guide.append("\n<i>Small tokens can fall fast. Targets are possibilities, not promises. Not financial advice.</i>")
    msgs.append("\n".join(guide))
    return [m if len(m) < 4000 else m[:3990] + "…" for m in msgs]
