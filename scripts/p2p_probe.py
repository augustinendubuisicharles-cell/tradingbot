"""One-off check: can the bot read naira (NGN) USDT P2P ads from GitHub's servers?
Prints the best few prices on each side per exchange, or the error."""
import json

import requests

H = {"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"}


def show(name, side, prices, err=None):
    print(f"{name:8s} {side:12s}", err if err else prices[:5], flush=True)


def bybit():
    for side, label in (("1", "sellers ask"), ("0", "buyers bid")):
        try:
            r = requests.post("https://api2.bybit.com/fiat/otc/item/online", headers=H, timeout=20, json={
                "userId": "", "tokenId": "USDT", "currencyId": "NGN", "payment": [], "side": side,
                "size": "10", "page": "1", "amount": "", "authMaker": False, "canTrade": False})
            items = r.json().get("result", {}).get("items", [])
            show("bybit", label, [float(i["price"]) for i in items], None if r.ok else f"HTTP {r.status_code}")
        except Exception as e:
            show("bybit", label, [], f"failed: {type(e).__name__} {str(e)[:120]}")


def binance():
    for side, label in (("BUY", "sellers ask"), ("SELL", "buyers bid")):
        try:
            r = requests.post("https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search", headers=H, timeout=20,
                              json={"fiat": "NGN", "page": 1, "rows": 10, "tradeType": side, "asset": "USDT",
                                    "countries": [], "proMerchantAds": False, "publisherType": None, "payTypes": []})
            data = r.json().get("data") or []
            show("binance", label, [float(d["adv"]["price"]) for d in data], None if r.ok else f"HTTP {r.status_code}")
        except Exception as e:
            show("binance", label, [], f"failed: {type(e).__name__} {str(e)[:120]}")


def okx():
    for side, label in (("sell", "sellers ask"), ("buy", "buyers bid")):
        try:
            r = requests.get("https://www.okx.com/v3/c2c/tradingOrders/books", headers=H, timeout=20, params={
                "quoteCurrency": "NGN", "baseCurrency": "USDT", "side": side, "paymentMethod": "all",
                "userType": "all", "showTrade": "false", "showFollow": "false", "showAlreadyTraded": "false",
                "isAbleFilter": "false"})
            data = (r.json().get("data") or {}).get(side, [])
            show("okx", label, [float(d["price"]) for d in data], None if r.ok else f"HTTP {r.status_code}")
        except Exception as e:
            show("okx", label, [], f"failed: {type(e).__name__} {str(e)[:120]}")


def bitget():
    for side, label in ((1, "sellers ask"), (2, "buyers bid")):
        try:
            r = requests.post("https://www.bitget.com/v1/p2p/pub/adv/queryAdvList", headers=H, timeout=20, json={
                "side": side, "pageNo": 1, "pageSize": 10, "coinCode": "USDT", "fiatCode": "NGN", "languageType": 0})
            body = r.json()
            rows = (body.get("data") or {}).get("dataList") or []
            show("bitget", label, [float(x.get("price", 0)) for x in rows],
                 None if r.ok and rows else f"HTTP {r.status_code} {json.dumps(body)[:160]}")
        except Exception as e:
            show("bitget", label, [], f"failed: {type(e).__name__} {str(e)[:120]}")


def raw():
    """Prints one full ad per exchange, to learn the field names."""
    try:
        r = requests.post("https://api2.bybit.com/fiat/otc/item/online", headers=H, timeout=20, json={
            "userId": "", "tokenId": "USDT", "currencyId": "NGN", "payment": [], "side": "1",
            "size": "3", "page": "1", "amount": "", "authMaker": False, "canTrade": False})
        print("RAW bybit", json.dumps(r.json()["result"]["items"][0])[:1500])
    except Exception as e:
        print("RAW bybit failed", e)
    try:
        r = requests.get("https://www.okx.com/v3/c2c/tradingOrders/books", headers=H, timeout=20, params={
            "quoteCurrency": "NGN", "baseCurrency": "USDT", "side": "sell", "paymentMethod": "all", "userType": "all"})
        print("RAW okx", json.dumps(r.json()["data"]["sell"][0])[:1500])
    except Exception as e:
        print("RAW okx failed", e)
    try:
        r = requests.post("https://www.bitget.com/v1/p2p/pub/adv/queryAdvList", headers=H, timeout=20, json={
            "side": 1, "pageNo": 1, "pageSize": 3, "coinCode": "USDT", "fiatCode": "NGN", "languageType": 0})
        print("RAW bitget", json.dumps(r.json()["data"]["dataList"][0])[:1500])
    except Exception as e:
        print("RAW bitget failed", e)


if __name__ == "__main__":
    raw()
    for f in (bybit, binance, okx, bitget):
        f()
