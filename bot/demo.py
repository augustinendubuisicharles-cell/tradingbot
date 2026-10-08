"""Made-up market and social data for previewing the dashboard offline."""
import random
from datetime import datetime, timedelta

from .social import Post

# coin: (current price, drift per candle, volatility per candle, 24h volume)
COINS = {
    "BTC": (64000, 0.0012, 0.008, 9e8), "ETH": (2600, 0.0010, 0.011, 5e8),
    "SOL": (150, 0.0018, 0.016, 2e8), "XRP": (0.55, -0.0004, 0.014, 1e8),
    "BNB": (580, 0.0003, 0.009, 8e7), "DOGE": (0.12, -0.0010, 0.022, 9e7),
    "ADA": (0.36, -0.0008, 0.017, 4e7), "AVAX": (27, 0.0002, 0.020, 3e7),
    "LINK": (11.5, 0.0014, 0.015, 3.5e7), "TON": (5.2, -0.0015, 0.013, 2.5e7),
    "SUI": (1.9, 0.0030, 0.028, 6e7), "DOT": (4.3, -0.0005, 0.016, 1.5e7),
    # emerging-token preview
    "FET": (1.35, 0.0025, 0.022, 2.4e7), "ONDO": (0.92, 0.0016, 0.015, 1.8e7),
    "VIRTUAL": (1.10, 0.0045, 0.040, 6e6), "PLUME": (0.11, -0.0030, 0.030, 3e6),
}
FUNDING = {"BTC": 0.0001, "ETH": 0.00008, "SOL": 0.0002, "SUI": 0.0009, "DOGE": 0.0004}


def _walk(seed: str, end_price: float, drift: float, vol: float, n: int, step: timedelta,
          now: datetime) -> list[list[float]]:
    rng = random.Random(seed)
    rets = [rng.gauss(drift, vol) for _ in range(n)]
    price = end_price
    closes = []
    for r in reversed(rets):  # walk backwards so the last close equals end_price
        closes.append(price)
        price /= 1 + r
    closes.reverse()
    rows, prev = [], closes[0] / (1 + rets[0])
    start = now - step * n
    for i, c in enumerate(closes):
        wiggle = abs(rng.gauss(0, vol / 2))
        rows.append([(start + step * i).timestamp() * 1000, prev,
                     max(prev, c) * (1 + wiggle), min(prev, c) * (1 - wiggle), c, 1000.0])
        prev = c
    return rows


class FakeMarket:
    def __init__(self, now: datetime):
        self.now = now

    def tickers(self, coins):
        out = {}
        for c in coins:
            if c in COINS:
                price, drift, _, volume = COINS[c]
                out[c] = {"price": price, "change_24h": drift * 6 * 100 * 1.7, "volume_24h": volume}
        return out

    def candles(self, coin, timeframe, limit=200):
        price, drift, vol, _ = COINS[coin]
        if timeframe == "1d":
            return _walk(coin + "d", price, drift * 6, vol * 2.4, limit, timedelta(days=1), self.now)
        return _walk(coin, price, drift, vol, limit, timedelta(hours=4), self.now)

    def futures_signals(self, coin):
        return {"funding_rate": FUNDING.get(coin, 0.0001), "open_interest": COINS[coin][3] * 3}


SAMPLE_POSTS = [
    ("telegram", "demo_trader_a", "$SOL breakout above range high, bullish. Accumulating on dips 🚀", 1.5, 52000),
    ("telegram", "demo_trader_a", "BTC reclaimed 63k support, ETF inflows strong. Still bullish into the weekend", 3, 61000),
    ("telegram", "demo_trader_b", "SUI pumping hard but funding is overheated, careful chasing here", 2, 18000),
    ("telegram", "demo_trader_b", "$DOGE looks weak, rejected at resistance again. Bearish below 0.13", 5, 15000),
    ("reddit", "r/CryptoCurrency", "Ethereum staking inflows hit a new high this month", 6, 820),
    ("reddit", "r/CryptoCurrency", "Cardano ADA keeps bleeding, is it time to sell?", 4, 310),
    ("reddit", "r/CryptoMarkets", "Chainlink $LINK quietly breaking out, uptrend on the daily", 9, 260),
    ("news", "CoinDesk", "Bitcoin ETF inflows top $500M as BTC rallies toward record", 7, 0),
    ("news", "Cointelegraph", "Toncoin slides as network outage sparks FUD", 10, 0),
    ("news", "Decrypt", "Solana DEX volumes surge as SOL rallies 8%", 12, 0),
]


def fake_posts(now: datetime) -> list[Post]:
    return [Post(source=s, author=a, text=t, ts=now - timedelta(hours=h), url="", engagement=e)
            for s, a, t, h, e in SAMPLE_POSTS]


def fake_emerging() -> tuple[list[dict], dict]:
    """CoinGecko-shaped rows for the emerging-token preview."""
    rows = [
        ("FET", "Artificial Superintelligence Alliance", "AI", 1.2e9, 9.0, 14.0, 0.75),
        ("VIRTUAL", "Virtuals Protocol", "AI", 7.2e8, 22.0, 41.0, 0.65),
        ("ONDO", "Ondo", "RWA", 1.4e9, 6.0, 11.0, 0.32),
        ("PLUME", "Plume", "RWA", 3.1e8, -12.0, -25.0, 0.30),
    ]
    picks = [{"symbol": s, "name": n, "narrative": nar, "market_cap": mc, "current_price": COINS[s][0],
              "total_volume": COINS[s][3] * 3, "price_change_percentage_7d_in_currency": d7,
              "price_change_percentage_30d_in_currency": d30, "circulating_supply": circ,
              "total_supply": 1.0, "ath_change_percentage": -70.0}
             for s, n, nar, mc, d7, d30, circ in rows]
    narratives = {"AI": {"change_24h": 3.4, "categories": ["Artificial Intelligence (AI)"]},
                  "RWA": {"change_24h": 1.1, "categories": ["Real World Assets (RWA)"]}}
    return picks, narratives
