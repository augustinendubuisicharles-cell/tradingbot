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


def top_holder_share(info: dict) -> float | None:
    """Share of supply held by the 10 biggest wallets, leaving out locked, burnt and labelled
    (exchange or pool) wallets, which hold coins for many people."""
    holders = info.get("holders") or []
    if not holders:
        return None
    dead = ("0x000000000000000000000000000000000000dead", "0x0000000000000000000000000000000000000000")
    pcts = []
    for h in holders:
        if str(h.get("is_locked", "0")) == "1" or str(h.get("address", "")).lower() in dead or h.get("tag"):
            continue
        try:
            pcts.append(float(h.get("percent") or 0))
        except ValueError:
            continue
    return sum(sorted(pcts, reverse=True)[:10])


def contract_check(platforms: dict | None) -> tuple[bool | None, str, list[str]]:
    """GoPlus token-security scan. (passed, summary, warnings); passed None = couldn't check.
    The top-10 holder share, when known, is left in `contract_check.last_share`."""
    contract_check.last_share = None
    platforms = {k: v for k, v in (platforms or {}).items() if k and v}
    if not platforms:
        return True, "native coin of its own blockchain (no token contract to exploit)", []
    chain_key = next((k for k in platforms if k in CHAINS), None)
    if chain_key is None and "solana" in platforms:
        return solana_check(platforms["solana"])
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
    contract_check.last_share = top_holder_share(info)
    flag = lambda k: str(info.get(k, "0")) == "1"  # noqa: E731
    tax = lambda k: float(info.get(k) or 0)  # noqa: E731
    fails = []
    if flag("is_honeypot"): fails.append("honeypot (can't sell)")
    if flag("cannot_sell_all"): fails.append("blocks selling your full balance")
    if flag("owner_change_balance"): fails.append("owner can change balances")
    if flag("hidden_owner"): fails.append("hidden owner")
    if flag("selfdestruct"): fails.append("can self-destruct")
    if str(info.get("is_open_source", "1")) == "0": fails.append("code not public")
    if flag("slippage_modifiable") or flag("personal_slippage_modifiable"):
        fails.append("owner can change the trading fees")
    if tax("buy_tax") > 0.05 or tax("sell_tax") > 0.05:
        fails.append(f"high tax (buy {tax('buy_tax'):.0%}, sell {tax('sell_tax'):.0%})")
    warns = []
    if flag("is_mintable"): warns.append("team can mint more tokens")
    if flag("transfer_pausable"): warns.append("transfers can be paused")
    if flag("is_proxy"): warns.append("upgradeable contract")
    if fails:
        return False, "contract scan failed: " + ", ".join(fails), warns
    return True, f"contract scanned clean on {chain_key.replace('-', ' ')}", warns


def solana_check(mint: str) -> tuple[bool | None, str, list[str]]:
    """GoPlus scan of a Solana (SPL) token: who can freeze, close or change balances."""
    try:
        r = requests.get("https://api.gopluslabs.io/api/v1/solana/token_security",
                         params={"contract_addresses": mint}, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        res = r.json().get("result") or {}
        info = res.get(mint) or next(iter(res.values()), None)
        time.sleep(2)
    except Exception as e:  # noqa: BLE001
        log.warning("goplus solana check failed for %s: %s", mint, e)
        return None, "Solana token scanner unavailable", []
    if not info:
        return None, "Solana token not in the scanner's database yet", []
    contract_check.last_share = top_holder_share(info)

    def on(key: str) -> bool:
        v = info.get(key)
        v = v.get("status") if isinstance(v, dict) else v
        return str(v) == "1"
    fails = [label for key, label in (("freezable", "team can freeze wallets"),
                                      ("balance_mutable_authority", "someone can change balances"),
                                      ("closable", "token accounts can be closed by the team"),
                                      ("non_transferable", "can't be transferred"))
             if on(key)]
    warns = ["team can mint more tokens"] if on("mintable") else []
    if fails:
        return False, "Solana token scan failed: " + ", ".join(fails), warns
    return True, "Solana token scanned clean (no freeze or balance-change powers)", warns


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

def _project_check(base: dict) -> tuple:
    if base.get("is_meme") or "meme" in (base.get("narrative") or "").lower():
        return ("Real product", False, "meme coin: nothing behind it but attention")
    return ("Real product", bool(base.get("homepage")) or None,
            ("has a website" if base.get("homepage") else "no website listed")
            + (f", {base['commits_4w']} code updates in 4 weeks" if base.get("commits_4w") else ""))


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
        ("Spread across many holders",
         (base["top10_share"] <= 0.5) if base.get("top10_share") is not None else None,
         f"top 10 private wallets hold {base['top10_share']:.0%} of supply" if base.get("top10_share") is not None
         else "holder data not available"),
        _project_check(base),
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

# Timing settings. The weekly backtest may replace these with better-tested values
# (data/tuned.json), but only if they also beat these on recent months it didn't tune on.
DEFAULT_TIMING = {"trend_min": 55, "rsi_max": 70, "stretch_atr": 1.0, "stop_atr": 2.5,
                  "tp1_r": 2.0, "tp2_r": 4.0, "exit_on_ema20": True, "btc_filter": False}


def timing_params(cfg: dict) -> dict:
    return {**DEFAULT_TIMING, **(cfg.get("emerging", {}).get("timing") or {})}


def timing_plan(c4h: list[list[float]], c1d: list[list[float]], trend: float, rating: str,
                risk_cfg: dict, max_position_pct: float, params: dict | None = None,
                ath: float | None = None, btc_up: bool | None = None) -> dict:
    """When to get in, where the stop goes, and when to get out."""
    tp = {**DEFAULT_TIMING, **(params or {})}
    spike = spike_info(c4h)
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
    if tp["btc_filter"] and btc_up is False:
        return {**plan, "status": "Avoid", "action": "Bitcoin is in a downtrend, and small coins rarely rise "
                                                     "against it. Wait for Bitcoin to turn up."}

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

    datr = analysis.atr(c1d) if len(c1d) > 15 else None
    min_dist = datr if datr else 0.0
    month = closes[-1] / closes[-181] - 1 if len(closes) > 180 and closes[-181] else None
    if spike and spike["base_high"] < price:
        return spike_plan(plan, spike, price, rating, risk_cfg, max_position_pct, ath, tp, min_dist)
    low7 = min(c[3] for c in c4h[-42:])
    if month is not None and month > 1.0 and trend >= tp["trend_min"]:
        status, entry = "Wait for pullback", low7 * 1.02
        entry_why = "just above this week's low, a level that actually traded"
        action = (f"Up {month:.0%} in a month, so too stretched to buy now. Only buy a pullback to about "
                  f"{fmt_price(entry)}, near this week's low.")
    elif trend >= tp["trend_min"] and r is not None and r <= tp["rsi_max"] and price - e20 <= tp["stretch_atr"] * a:
        status, entry = "Enter zone", price
        entry_why = "today's price, close to its 20-period average, so not chasing"
        action = "Uptrend confirmed and not overextended. A good time to position, at today's price."
    elif trend >= tp["trend_min"]:
        status, entry = "Wait for pullback", e20
        entry_why = "its 20-period average, where pullbacks in this uptrend have been bought"
        action = (f"Uptrend, but price is stretched. Set a buy around {fmt_price(e20)} "
                  "(its 20-period average) instead of chasing.")
    else:
        status, entry = "Watch for breakout", high20 * 1.005
        entry_why = "just above the recent high; only on a 4h close above it"
        action = (f"Not trending yet. Buy only if a 4h candle closes above {fmt_price(high20)} "
                  "(the recent high) on rising volume.")

    stop = plan_stop(entry, swing_low, a, tp["stop_atr"], status == "Enter zone", min_dist)
    risk_per_unit = entry - stop
    if risk_per_unit <= 0:
        return {**plan, "status": "Avoid", "action": "No sensible stop-loss level right now."}
    why: list[str] = []
    tp1, tp2 = chart_targets(max(c[2] for c in c4h[-42:]), max(c[2] for c in c4h[-180:]),
                             entry, risk_per_unit, tp, ath, why)
    day_pct = f"{100 * min_dist / entry:.0f}%" if min_dist else None
    stop_why = ("below the recent swing low" if status == "Enter zone" else f"{tp['stop_atr']:g}× the average 4h move below entry")
    if day_pct:
        stop_why += f", and at least one normal day's swing away (about {day_pct})"
    return {**plan, **_sizing(entry, stop, rating, risk_cfg, max_position_pct),
            "status": status, "action": action, "entry": entry, "stop": stop, "tp1": tp1, "tp2": tp2,
            "why": {"entry": entry_why, "stop": stop_why, "tp1": why[0], "tp2": why[1]},
            "exit_rule": _exit_rule(entry, tp)}


def chart_targets(high7: float, high30: float, entry: float, risk: float, tp: dict,
                  ath: float | None, why: list | None = None) -> tuple[float, float]:
    """Targets at real chart levels (this week's high, then this month's high) when they
    are far enough away; otherwise fixed multiples of the risk. Never above the old high."""
    on_level1 = high7 >= entry + 1.0 * risk
    tp1 = high7 if on_level1 else entry + tp["tp1_r"] * risk
    on_level2 = high30 >= tp1 + 0.5 * risk
    tp2 = high30 if on_level2 else max(entry + tp["tp2_r"] * risk, tp1 + risk)
    capped = bool(ath and tp1 < ath < tp2)
    if capped:
        tp2 = ath
    if why is not None:
        why += ["this week's high" if on_level1 else f"{tp['tp1_r']:g}× the risk (no chart level close enough)",
                "the old all-time high" if capped else "this month's high" if on_level2
                else "a multiple of the risk (no chart level above; treat as a stretch)"]
    return tp1, tp2


def _sizing(entry: float, stop: float, rating: str, risk_cfg: dict, max_position_pct: float) -> dict:
    account = risk_cfg["account_size"]
    pct = RISK_PCT[rating]
    risk_per_unit = entry - stop
    qty = min(account * pct / 100 / risk_per_unit, account * max_position_pct / 100 / entry)
    return {"risk_pct": pct, "notional": qty * entry, "max_loss": qty * risk_per_unit}


def _exit_rule(entry: float, tp: dict) -> str:
    return (f"Sell half at target 1 and move the stop to {fmt_price(entry)} (breakeven). "
            "Exit the rest at target 2"
            + (", on a 4h close below the 20-period average," if tp["exit_on_ema20"] else "")
            + " or after 10 days if it hasn't moved.")


def spike_plan(plan: dict, spike: dict, price: float, rating: str, risk_cfg: dict,
               max_position_pct: float, ath: float | None, tp: dict, min_dist: float = 0.0) -> dict:
    """After a one-day spike: don't chase. Buy only a pullback to the top of the base it
    broke out of, stop under that base, first target the spike high, then the old high."""
    entry = spike["base_high"]
    stop = min(spike["base_low"] * 0.99, entry - min_dist)
    tp1 = spike["spike_high"]
    tp2 = ath if ath and ath > tp1 * 1.05 else tp1 + (tp1 - entry)
    if entry - stop <= 0 or (entry - stop) / entry > 0.30:
        return {**plan, "status": "Avoid", "action": (
            f"Just spiked {spike['change_24h']:+.0f}% in a day with no sensible stop level. "
            "Let it settle before considering it.")}
    skip_above = entry + 0.5 * (price - entry)
    return {**plan, **_sizing(entry, stop, rating, risk_cfg, max_position_pct),
            "status": "Wait for pullback", "spike": True,
            "action": (f"Don't buy the spike (+{spike['change_24h']:.0f}% in 24h). Set a limit buy at "
                       f"{fmt_price(entry)}, the top of the range it broke out of. "
                       f"Skip it if price is still above {fmt_price(skip_above)} after a day or two."),
            "entry": entry, "stop": stop, "tp1": tp1, "tp2": tp2,
            "why": {"entry": "the top of the range it traded in before the spike",
                    "stop": "below that pre-spike range: a close under it means the breakout failed",
                    "tp1": "the spike high", "tp2": "the old all-time high" if ath and tp2 == ath
                    else "the spike high plus the same distance again (a stretch)"},
            "exit_rule": (f"Stop below the pre-spike range at {fmt_price(stop)}: a close under it means the "
                          f"breakout failed. Sell half at {fmt_price(tp1)} (the spike high) and move the stop "
                          "to your buy price; sell the rest at "
                          + (f"{fmt_price(tp2)}, the old all-time high." if ath and tp2 == ath
                             else f"{fmt_price(tp2)}.")),
            }


def plan_stop(entry: float, swing_low: float, a: float, stop_atr: float, at_market: bool,
              min_dist: float = 0.0) -> float:
    """Under the recent swing low, but never closer than 1 or further than `stop_atr` average 4h
    ranges, and always outside a normal day's swing (`min_dist`), so noise doesn't stop you out."""
    stop = max(min(swing_low, entry - a), entry - stop_atr * a) if at_market else entry - stop_atr * a
    return min(stop, entry - min_dist)



def mention_pattern(sym: str, name: str) -> re.Pattern:
    """$SYM, #SYM, SYM/USDT, or the full name when it's distinctive (6+ letters)."""
    alts = [rf"[$#]{re.escape(sym)}\b", rf"\b{re.escape(sym)}/?USDT\b"]
    if len(name) >= 6:
        alts.append(rf"\b{re.escape(name)}\b")
    return re.compile("|".join(alts), re.IGNORECASE)




def spike_info(c4h: list[list[float]]) -> dict | None:
    """A one-day spike: up 25%+ in 24h, or volume 4×+ normal while up 15%+.
    Returns the pre-spike base (the 3 days before) and the spike high."""
    if len(c4h) < 30:
        return None
    price, day_ago = c4h[-1][4], c4h[-7][4]
    change = price / day_ago - 1 if day_ago else 0
    surge = volume_surge(c4h) or 1.0
    if not (change >= 0.25 or (surge >= 4 and change >= 0.15)):
        return None
    base = c4h[-25:-6]
    return {"change_24h": change * 100, "surge": surge,
            "base_high": max(c[2] for c in base), "base_low": min(c[3] for c in base),
            "spike_high": max(c[2] for c in c4h[-6:]), "spike_low": min(c[3] for c in c4h[-6:])}


def potential_score(cg: dict, base: dict, trend: float, surge: float | None, mentions: int,
                    trending: bool, ecfg: dict, spike: dict | None = None) -> tuple[float, list[str]]:
    """0..100: how much room and fuel the token has, and how early it still is.
    Looks at the last 30 days and the all-time range, not just today."""
    notes = []
    score = 0.25 * trend
    ch7 = cg.get("price_change_percentage_7d_in_currency") or 0.0
    ch30 = cg.get("price_change_percentage_30d_in_currency") or 0.0
    rel = max(-30.0, min(30.0, 0.6 * ch7 + 0.4 * ch30))
    score += 0.15 * (50 + rel * 50 / 30)
    if ch7 >= 5:
        notes.append(f"Up {ch7:.0f}% this week and {ch30:+.0f}% over 30 days")
    # Already-extended moves have less room left.
    if ch30 > 100:
        score -= 10
        notes.append(f"Already up {ch30:.0f}% in a month, so much of the move may be done")
    if spike:
        score -= 10
        notes.append(f"Today is a spike (+{spike['change_24h']:.0f}% in 24h, volume {spike['surge']:.0f}× normal), "
                     "not steady buying. Spikes often give part of it back")
    elif surge is not None:
        score += 0.10 * max(0.0, min(100.0, (surge - 0.5) * 66))
        if surge >= 1.5:
            notes.append(f"Trading volume has risen steadily to {surge:.1f}× normal over the past day")
    nch = base.get("narrative_change")
    if nch is not None:
        score += 0.15 * max(0.0, min(100.0, 50 + nch * 5))
        if nch > 2:
            notes.append(f"Its sector ({base['narrative']}) is rising today ({nch:+.1f}%)")
    # Early: hard to prove. Few watchers is only a weak hint, and a big recent run means
    # the market has already noticed it.
    watchers = base.get("watchers")
    early = 40.0 if watchers is None else max(0.0, 80 - watchers / 250)
    if trending:
        early -= 30
    if mentions > 3:
        early -= 20
    if ch30 > 80 or spike:
        early = min(early, 15.0)
    score += 0.15 * max(0.0, early)
    if early >= 50 and watchers is not None:
        notes.append(f"Possibly still early: {watchers:,} CoinGecko watchers and no big run yet (a hint, not proof)")
    if mentions:
        notes.append(f"Traders are starting to talk about it ({mentions} posts)")
    mcap = cg.get("market_cap") or ecfg["max_market_cap"]
    room = max(0.0, min(100.0, 100 * (ecfg["max_market_cap"] - mcap) / (ecfg["max_market_cap"] - 30e6)))
    score += 0.10 * room
    ath, atl = cg.get("ath") or 0, cg.get("atl") or 0
    price = cg.get("current_price") or 0
    if ath and price and atl:
        pos = (price - atl) / (ath - atl) if ath > atl else 1
        notes.append(f"Price is {pos:.0%} of the way from its all-time low ({fmt_price(atl)}) "
                     f"to its high ({fmt_price(ath)})")
    age = base.get("age_days")
    if age is not None and not base.get("age_capped"):
        score += 0.05 * (100 if age <= 60 else 60 if age <= 180 else 20)
        if age <= 180:
            notes.append(f"New listing: on Bitget for only {age} days")
    else:
        score += 0.05 * 20
    return round(max(0.0, min(100.0, score)), 1), notes


def scenarios(cg: dict, plan: dict) -> list[dict]:
    """Bear / base / stretch / bull outcomes from the entry, with the market cap each implies."""
    if "entry" not in plan:
        return []
    entry, price, mcap = plan["entry"], plan["price"], cg.get("market_cap") or 0
    rows = [("Bear", plan["stop"], "stop-loss hit"),
            ("Base", plan["tp1"], "first target, if the setup works"),
            ("Stretch", plan["tp2"], "second target, if the trend runs")]
    ath = cg.get("ath") or 0
    if plan["tp2"] * 1.02 < ath <= plan["tp2"] * 1.5:
        rows.append(("Old high", ath, "its all-time high: a reference, not a target"))
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
    spike = spike_info(c4h)
    pot, pot_notes = potential_score(cg, base, trend, volume_surge(c4h), mentions, sym in trending, ecfg, spike)

    plan = timing_plan(c4h, c1d, trend, rating, cfg["risk"], ecfg.get("max_position_pct", 10),
                       timing_params(cfg), ath=cg.get("ath"), btc_up=base.get("btc_up"))
    surge = volume_surge(c4h)
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
        # Inputs saved with each logged pick, so the bot can later learn which ones predicted wins.
        "features": {"potential": pot, "risk_score": risk, "trend": trend, "atr_pct": atr_pct,
                     "change_7d": cg.get("price_change_percentage_7d_in_currency"),
                     "change_30d": cg.get("price_change_percentage_30d_in_currency"),
                     "market_cap": cg.get("market_cap"), "watchers": base.get("watchers"),
                     "age_days": base.get("age_days"), "volume_surge": surge, "mentions": mentions,
                     "narrative_change": base.get("narrative_change"), "rsi": plan.get("rsi"),
                     "unknown_checks": len([c for c in checks if c["ok"] is None])},
    }


BUY_STATES = ("Enter zone", "Wait for pullback", "Watch for breakout")


def order(tokens: list[dict]) -> list[dict]:
    """Safe first, then ones with a buy setup, then fully verified, then by growth potential."""
    return sorted(tokens, key=lambda t: (not t["safe"], t["plan"]["status"] not in BUY_STATES,
                                         len(t["unknown_checks"]) > 1, -t["potential"]))


def _bitget_price_matches(cg: dict, ticker: dict) -> bool:
    p = cg.get("current_price") or 0
    return not p or abs(ticker["price"] / p - 1) <= 0.15


def btc_uptrend(mkt) -> bool | None:
    try:
        trend, _ = analysis.trend_score(mkt.candles("BTC", "4h", 200), mkt.candles("BTC", "1d", 120))
        return trend >= 50
    except Exception:  # noqa: BLE001
        return None


def _rate_bases(bases: list[dict], mkt, cfg: dict, posts_text: list[str], trending: list[str]) -> list[dict]:
    tickers = mkt.tickers([b["symbol"] for b in bases])
    btc_up = btc_uptrend(mkt) if timing_params(cfg)["btc_filter"] else None
    bases = [{**b, "btc_up": btc_up} for b in bases]
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
        share = contract_check.last_share
        bases.append({
            "symbol": cg["symbol"], "cg": cg, "narrative": narrative,
            "narrative_change": cat_change.get(narrative),
            "watchers": det["watchers"], "homepage": det["homepage"], "commits_4w": det["commits_4w"],
            "age_days": len(daily), "age_capped": len(daily) >= 300,
            "contract_ok": ok, "contract_note": note, "contract_warnings": warns,
            "top10_share": share, "is_meme": any("meme" in c.lower() for c in det["categories"]),
        })
    scan = {"as_of": datetime.now(timezone.utc).isoformat(), "bases": bases,
            "hot_narratives": [{"name": c["name"], "change_24h": round(c["market_cap_change_24h"], 1)}
                               for c in hot]}
    return _rate_bases(bases, mkt, cfg, posts_text, trending), scan


def rerate(scan: dict, mkt, cfg: dict, posts_text: list[str], trending: list[str]) -> list[dict]:
    """4-hourly: fresh Bitget prices and timing for this morning's picks (no CoinGecko calls)."""
    return _rate_bases(scan.get("bases", []), mkt, cfg, posts_text, trending)


# ---------- track record of picks ----------

def log_picks(history: list[dict], tokens: list[dict], now: datetime, params: dict) -> int:
    """Records safe picks with a buy setup, once per token until that trade is over."""
    import uuid
    active = {h["symbol"] for h in history if h["status"] in ("open", "pending")}
    added = 0
    for t in tokens:
        p = t["plan"]
        if not t["safe"] or p["status"] not in ("Enter zone", "Wait for pullback") or t["symbol"] in active:
            continue
        history.append({
            "id": uuid.uuid4().hex[:8], "symbol": t["symbol"], "created": now.isoformat(),
            "kind": "market" if p["status"] == "Enter zone" else "limit",
            "entry": p["entry"], "stop": p["stop"], "tp1": p["tp1"], "tp2": p["tp2"],
            "exit_on_ema20": params["exit_on_ema20"], "narrative": t["narrative"],
            "risk_rating": t["risk_rating"], "potential": t["potential"], "features": t.get("features", {}),
            "status": "open" if p["status"] == "Enter zone" else "pending",
        })
        added += 1
    return added


def settle_picks(history: list[dict], candles_by_symbol: dict[str, list]) -> None:
    """Replays each unfinished pick against the candles since it was made."""
    from .tracker import manage_trade
    for h in history:
        if h["status"] not in ("open", "pending"):
            continue
        c4h = candles_by_symbol.get(h["symbol"])
        if not c4h:
            continue
        created_ms = datetime.fromisoformat(h["created"]).timestamp() * 1000
        start = max((i for i, c in enumerate(c4h) if c[0] <= created_ms), default=None)
        if start is None:
            continue
        res = manage_trade(c4h, start, h["entry"], h["stop"], h["tp1"], h["tp2"],
                           limit=h["kind"] == "limit", exit_on_ema20=h.get("exit_on_ema20", True))
        h["status"] = res["status"]
        for k in ("result_r", "unrealized_r", "half_taken"):
            if k in res:
                h[k] = res[k]
        if "closed_i" in res:
            h["closed"] = datetime.fromtimestamp(c4h[res["closed_i"]][0] / 1000, tz=timezone.utc).isoformat()


def picks_record(history: list[dict]) -> dict:
    closed = [h for h in history if "result_r" in h and h["status"] != "never filled"]
    wins = [h for h in closed if h["result_r"] > 0]
    return {
        "total": len(history),
        "open": sum(h["status"] in ("open", "pending") for h in history),
        "closed": len(closed),
        "win_rate": round(100 * len(wins) / len(closed)) if closed else None,
        "avg_r": round(sum(h["result_r"] for h in closed) / len(closed), 2) if closed else None,
        "total_r": round(sum(h["result_r"] for h in closed), 2),
    }


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
    if p.get("spike"):
        return f"🟡 <b>WAIT, don't chase the spike</b>\n{p['action']}"
    if p["status"] == "Wait for pullback":
        return (f"🟡 <b>WAIT, then buy at {fmt_price(p['entry'])}</b>\n"
                f"It's running hot at {fmt_price(p['price'])}. Set a buy order lower; don't chase it.")
    return (f"🟡 <b>WAIT for a breakout above {fmt_price(p['entry'])}</b>\n"
            f"Now {fmt_price(p['price'])}. Buy only if a 4-hour candle closes above that level.")


def _pick_message(i: int, t: dict) -> str:
    """A trade first (level, stop, two exits, each with its reason), then the checks,
    then the story, which is context and never a reason to buy on its own."""
    import html
    esc = lambda x: html.escape(str(x), quote=False)  # noqa: E731
    p = t["plan"]
    why = p.get("why", {})
    passed, total = sum(1 for c in t["safety"] if c["ok"]), len(t["safety"])
    nums = "1️⃣ 2️⃣ 3️⃣ 4️⃣ 5️⃣ 6️⃣ 7️⃣ 8️⃣ 9️⃣".split()
    lines = [f"{nums[i - 1] if i <= 9 else str(i) + '.'} <b>{t['symbol']}</b> ({esc(t['name'])})",
             f"{esc(t['narrative'])} · market cap {_mcap(t['market_cap'])} · now {fmt_price(p['price'])}",
             "", _plan_line(t)]
    if t["scenarios"]:
        sc = {s["case"]: s for s in t["scenarios"]}
        lines += ["", "<b>The trade</b>",
                  f"• Buy at {fmt_price(p['entry'])}: {esc(why.get('entry', ''))}",
                  f"• Stop {fmt_price(p['stop'])} ({sc['Bear']['pct']:+.0f}%): {esc(why.get('stop', ''))}",
                  f"• Sell half at {fmt_price(p['tp1'])} ({sc['Base']['pct']:+.0f}%): {esc(why.get('tp1', ''))}. "
                  "Then move the stop to your buy price.",
                  f"• Sell the rest at {fmt_price(p['tp2'])} ({sc['Stretch']['pct']:+.0f}%): {esc(why.get('tp2', ''))}",
                  f"• Size: about {p['notional']:,.0f} USDT, so the stop costs about {p['max_loss']:,.0f} USDT"]
    flags = [c for c in t["safety"] if not c["ok"]]
    lines += ["", f"🛡 <b>Checks: {passed} of {total} passed</b> · risk {RISK_WORDS[t['risk_rating']].lower()}"]
    lines += [f"{'❌' if c['ok'] is False else '❔'} {esc(c['name'])}: {esc(c['detail'])}" for c in flags]
    lines += [f"⚠️ {esc(w)}" for w in t["risk_notes"] if "unlocked" in w or "mint" in w or "upgradeable" in w]
    if t["potential_notes"]:
        lines += ["", f"<i>Context, not a reason to buy (growth score {t['potential']:.0f}/100)</i>"]
        lines += [f"• {esc(n)}" for n in t["potential_notes"][:4]]
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
             "• <b>The trade</b> is what to act on: a buy at a level that actually traded, a stop past "
             "normal daily noise, and two exits at chart levels.",
             "• <b>Context</b> (growth score, weekly gains, sector moves) is the story. It is never a reason to buy on its own.",
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
               "Enough trading history": "too new to judge",
               "Spread across many holders": "a few wallets own most of the supply",
               "Real product": "meme coins with nothing behind them"}.get(top, top.lower())
        guide += ["", f"🚫 <b>Rejected today:</b> {', '.join(t['symbol'] for t in rejected)}. "
                      f"The most common reason was {why}."]
    if dashboard_url:
        guide.append(f'\n<a href="{html.escape(dashboard_url)}">Open the dashboard for full details</a>')
    guide.append("\n<i>Small tokens can fall fast. Targets are possibilities, not promises. Not financial advice.</i>")
    msgs.append("\n".join(guide))
    return [m if len(m) < 4000 else m[:3990] + "…" for m in msgs]
