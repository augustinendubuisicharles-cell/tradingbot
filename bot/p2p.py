"""Naira P2P price scanner: finds where USDT can be bought cheapest and sold
dearest across Bybit, OKX and Bitget P2P, using only ads a normal user can
actually take (enough size, established trader, no new-user-only promos).

It only alerts; every P2P trade needs a manual bank transfer and confirmation.
Binance P2P is not reachable from GitHub's servers.
"""
import html
import json
import logging
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)
H = {"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"}

TICKET = 200_000          # naira per trade the ad must accept (about $145)
MIN_ORDERS = 100          # trader's completed orders
MIN_RATE = 0.95           # trader's completion rate
TRANSFER_COST = 1.5       # USDT to move coins between exchanges (TRC20 withdrawal, roughly)
ALERT_GAP = 1.0           # % profit after costs worth a Telegram
REPEAT_HOURS = 3


def _ad(ex, side, price, lo, hi, orders, rate, name, pay, usdt=None):
    return {"ex": ex, "side": side, "price": float(price), "min": float(lo or 0), "max": float(hi or 0),
            "orders": int(float(orders or 0)), "rate": float(rate) if rate not in (None, "") else None,
            "name": (name or "").strip(), "pay": pay, "usdt": float(usdt) if usdt else None}


def bybit() -> list[dict]:
    out = []
    for code, side in (("1", "ask"), ("0", "bid")):
        r = requests.post("https://api2.bybit.com/fiat/otc/item/online", headers=H, timeout=20, json={
            "userId": "", "tokenId": "USDT", "currencyId": "NGN", "payment": [], "side": code,
            "size": "30", "page": "1", "amount": str(TICKET), "authMaker": False, "canTrade": False})
        for i in r.json().get("result", {}).get("items", []):
            out.append(_ad("Bybit", side, i["price"], i.get("minAmount"), i.get("maxAmount"), i.get("finishNum"),
                           (i.get("recentExecuteRate") or 0) / 100, i.get("nickName"), "", i.get("lastQuantity")))
    return out


def okx() -> list[dict]:
    out = []
    for code, side in (("sell", "ask"), ("buy", "bid")):
        r = requests.get("https://www.okx.com/v3/c2c/tradingOrders/books", headers=H, timeout=20, params={
            "quoteCurrency": "NGN", "baseCurrency": "USDT", "side": code, "paymentMethod": "all",
            "userType": "all", "quoteMinAmountPerOrder": str(TICKET)})
        for d in (r.json().get("data") or {}).get(code, []):
            if float(d.get("minCompletedOrderQuantity") or 0) > 0 or d.get("verificationRequired"):
                continue                          # ad restricted to experienced takers
            out.append(_ad("OKX", side, d["price"], d.get("quoteMinAmountPerOrder"), d.get("quoteMaxAmountPerOrder"),
                           d.get("completedOrderQuantity"), d.get("completedRate"), d.get("nickName"),
                           ", ".join(d.get("paymentMethods") or []), d.get("availableAmount")))
    return out


def bitget() -> list[dict]:
    out = []
    for code, side in ((1, "ask"), (2, "bid")):
        r = requests.post("https://www.bitget.com/v1/p2p/pub/adv/queryAdvList", headers=H, timeout=20, json={
            "side": code, "pageNo": 1, "pageSize": 30, "coinCode": "USDT", "fiatCode": "NGN", "languageType": 0})
        for d in (r.json().get("data") or {}).get("dataList") or []:
            if d.get("firstOrderExclusiveFlag") or "New User" in (d.get("templateTitle") or ""):
                continue                          # first-order promo, not repeatable
            out.append(_ad("Bitget", side, d["price"], d.get("minAmount"), d.get("maxAmount"), d.get("turnoverNum"),
                           d.get("turnoverRate"), d.get("nickName"),
                           ", ".join(p.get("paymethodName", "") for p in d.get("paymethodInfo") or []),
                           d.get("lastAmount")))
    return out


SOURCES = {"Bybit": bybit, "OKX": okx, "Bitget": bitget}


def usable(ad: dict) -> bool:
    ok = ad["min"] <= TICKET <= ad["max"] and ad["orders"] >= MIN_ORDERS
    if ad["rate"] is not None:
        ok &= ad["rate"] >= MIN_RATE
    if ad["usdt"] is not None:
        ok &= ad["usdt"] * ad["price"] >= TICKET
    return ok


def scan() -> dict:
    ads, health = [], {}
    for name, fn in SOURCES.items():
        try:
            got = fn()
            ads += got
            health[name] = f"ok ({len(got)} ads)"
        except Exception as e:  # one exchange failing shouldn't stop the others
            health[name] = f"failed: {type(e).__name__}"
            log.warning("p2p %s failed: %s", name, e)
    good = [a for a in ads if usable(a)]
    best = {}
    for ex in SOURCES:
        asks = [a for a in good if a["ex"] == ex and a["side"] == "ask"]
        bids = [a for a in good if a["ex"] == ex and a["side"] == "bid"]
        best[ex] = {"ask": min(asks, key=lambda a: a["price"], default=None),
                    "bid": max(bids, key=lambda a: a["price"], default=None)}
    return {"ads": len(ads), "usable": len(good), "best": best, "health": health,
            "routes": routes(best)}


def routes(best: dict) -> list[dict]:
    """Every buy-here, sell-there pair, with profit after the transfer cost."""
    out = []
    usdt = TICKET / 1400
    for a_ex, a in best.items():
        for b_ex, b in best.items():
            ask, bid = a["ask"], b["bid"]
            if not ask or not bid:
                continue
            cost = 0 if a_ex == b_ex else TRANSFER_COST / usdt
            gap = (bid["price"] / ask["price"] - 1 - cost) * 100
            out.append({"buy": a_ex, "sell": b_ex, "ask": ask, "bid": bid, "gap": round(gap, 2),
                        "naira": round(TICKET * gap / 100)})
    return sorted(out, key=lambda r: -r["gap"])


def alert_text(r: dict) -> str:
    a, b = r["ask"], r["bid"]
    e = html.escape
    move = "" if r["buy"] == r["sell"] else f"\n3. Send the USDT from {r['buy']} to {r['sell']} (TRC20, about 1-2 USDT fee)."
    return (f"💱 P2P gap: buy on {r['buy']}, sell on {r['sell']}: about {r['gap']:.1f}% "
            f"(₦{r['naira']:,} on ₦{TICKET:,})\n\n"
            f"1. Buy USDT on {r['buy']} P2P at ₦{a['price']:,.2f} from {e(a['name'])} "
            f"({a['orders']} orders, limits ₦{a['min']:,.0f}-₦{a['max']:,.0f}{', ' + e(a['pay']) if a['pay'] else ''}).\n"
            f"2. Sell it on {r['sell']} P2P at ₦{b['price']:,.2f} to {e(b['name'])} "
            f"({b['orders']} orders, limits ₦{b['min']:,.0f}-₦{b['max']:,.0f})." + move +
            "\n\nPrices move in minutes: check both ads are still there before paying. Use escrow only, "
            "never release USDT before the naira is in your account, and never pay from or to a third party.")


def run(state: dict, now: datetime | None = None) -> tuple[dict, list[str]]:
    """One scan. Returns the log row and any alerts to send."""
    now = now or datetime.now(timezone.utc)
    s = scan()
    row = {"time": now.isoformat(timespec="minutes"), "usable": s["usable"], "health": s["health"],
           "best": {ex: {k: (v["price"] if v else None) for k, v in d.items()} for ex, d in s["best"].items()},
           "routes": [{k: r[k] for k in ("buy", "sell", "gap")} for r in s["routes"][:4]]}
    msgs = []
    sent = state.setdefault("p2p_sent", {})
    for r in s["routes"]:
        if r["gap"] < ALERT_GAP:
            break
        key = f"{r['buy']}>{r['sell']}"
        last = sent.get(key)
        if last and (now - datetime.fromisoformat(last)).total_seconds() < REPEAT_HOURS * 3600:
            continue
        sent[key] = now.isoformat()
        msgs.append(alert_text(r))
        break                                    # one alert per scan is enough
    return row, msgs


def main() -> None:
    """GitHub Actions entry: python -m bot.p2p [--send]. State and the log live in
    the Actions cache so the every-30-minute runs don't fill git with commits."""
    import sys
    from pathlib import Path
    from .alerts import send_telegram
    logging.basicConfig(level=logging.INFO)
    data = Path(__file__).resolve().parent.parent / "data"
    sp, lp = data / "p2p_state.json", data / "p2p_log.jsonl"
    state = json.loads(sp.read_text()) if sp.exists() else {}
    row, msgs = run(state)
    print(json.dumps(row, indent=1))
    for m in msgs:
        print(m)
        if "--send" in sys.argv:
            send_telegram(m)
    data.mkdir(exist_ok=True)
    sp.write_text(json.dumps(state))
    with lp.open("a") as f:
        f.write(json.dumps(row) + "\n")


if __name__ == "__main__":
    main()
