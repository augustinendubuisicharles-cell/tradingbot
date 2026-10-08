"""Market data: prices and candles from the exchange, plus free market-wide gauges.

Everything here uses public endpoints, so no exchange API key is needed.
"""
import logging

import ccxt
import requests

log = logging.getLogger(__name__)
HTTP_TIMEOUT = 20


class Market:
    def __init__(self, exchange_id: str, quote: str):
        cls = getattr(ccxt, exchange_id)
        self.spot = cls({"enableRateLimit": True, "options": {"defaultType": "spot"}})
        self.perp = cls({"enableRateLimit": True, "options": {"defaultType": "swap"}})
        self.quote = quote

    def spot_symbol(self, coin: str) -> str:
        return f"{coin}/{self.quote}"

    def perp_symbol(self, coin: str) -> str:
        return f"{coin}/{self.quote}:{self.quote}"

    def tickers(self, coins: list[str]) -> dict[str, dict]:
        """Last price, 24h change % and 24h quote volume per coin."""
        raw = self.spot.fetch_tickers([self.spot_symbol(c) for c in coins])
        out = {}
        for coin in coins:
            t = raw.get(self.spot_symbol(coin))
            if not t or t.get("last") is None:
                continue
            out[coin] = {
                "price": float(t["last"]),
                "change_24h": float(t.get("percentage") or 0.0),
                "volume_24h": float(t.get("quoteVolume") or 0.0),
            }
        return out

    def candles(self, coin: str, timeframe: str, limit: int = 200) -> list[list[float]]:
        """OHLCV rows: [timestamp_ms, open, high, low, close, volume]."""
        return self.spot.fetch_ohlcv(self.spot_symbol(coin), timeframe, limit=limit)

    def futures_signals(self, coin: str) -> dict:
        """Funding rate and open interest from the perpetual market.

        Used only as a sentiment gauge (crowded longs vs shorts); the bot never
        suggests futures trades.
        """
        out = {"funding_rate": None, "open_interest": None}
        symbol = self.perp_symbol(coin)
        try:
            out["funding_rate"] = float(self.perp.fetch_funding_rate(symbol)["fundingRate"])
        except Exception as e:  # noqa: BLE001 - one missing gauge shouldn't stop the run
            log.warning("funding rate unavailable for %s: %s", coin, e)
        try:
            oi = self.perp.fetch_open_interest(symbol)
            value = oi.get("openInterestValue") or oi.get("openInterestAmount")
            out["open_interest"] = float(value) if value is not None else None
        except Exception as e:  # noqa: BLE001
            log.warning("open interest unavailable for %s: %s", coin, e)
        return out


def fear_greed() -> dict | None:
    """Crypto Fear & Greed index (0 = extreme fear, 100 = extreme greed)."""
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        d = r.json()["data"][0]
        return {"value": int(d["value"]), "label": d["value_classification"]}
    except Exception as e:  # noqa: BLE001
        log.warning("fear & greed unavailable: %s", e)
        return None


def trending_coins() -> list[str]:
    """Tickers currently trending on CoinGecko searches."""
    try:
        r = requests.get("https://api.coingecko.com/api/v3/search/trending", timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        return [c["item"]["symbol"].upper() for c in r.json().get("coins", [])]
    except Exception as e:  # noqa: BLE001
        log.warning("coingecko trending unavailable: %s", e)
        return []
