"""Free, offline sentiment scoring with VADER plus crypto slang."""
import re
from datetime import datetime, timezone

from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

from .ai import stance_score
from .social import Post, influence

# VADER's lexicon is general English; these words carry a clear direction in
# crypto talk. Scores are on VADER's -4..+4 scale.
CRYPTO_LEXICON = {
    "bullish": 2.5, "bearish": -2.5, "moon": 2.0, "mooning": 2.5, "pump": 1.5,
    "pumping": 1.8, "dump": -2.0, "dumping": -2.2, "rekt": -2.5, "breakout": 2.0,
    "breakdown": -2.0, "hodl": 1.5, "fud": -1.5, "ath": 2.0, "rug": -3.0,
    "rugged": -3.0, "scam": -3.0, "hack": -3.0, "hacked": -3.2, "exploit": -2.8,
    "liquidated": -2.0, "liquidation": -1.5, "accumulate": 1.5, "accumulating": 1.5,
    "undervalued": 1.8, "overvalued": -1.5, "rally": 2.0, "rallying": 2.0,
    "crash": -2.8, "crashing": -3.0, "capitulation": -2.0, "uptrend": 1.8,
    "downtrend": -1.8, "reversal": 0.5, "rejection": -1.2, "rejected": -1.2,
    "reclaim": 1.5, "reclaimed": 1.5, "inflows": 1.5, "outflows": -1.5,
    "approval": 1.5, "approved": 1.5, "delist": -2.5, "delisted": -2.5,
    "sec": -0.5, "lawsuit": -2.0, "etf": 1.0, "buy": 1.2, "sell": -1.2,
    "long": 0.8, "short": -0.8, "rocket": 2.0,  # VADER spells out emoji, e.g. 🚀
}

_analyzer = SentimentIntensityAnalyzer()
_analyzer.lexicon.update(CRYPTO_LEXICON)


# A trade call: a direction word plus at least one trade level.
_LONG = re.compile(r"\b(long|buy|bullish)\b", re.IGNORECASE)
_SHORT = re.compile(r"\b(short|sell|bearish)\b", re.IGNORECASE)
_LEVELS = re.compile(r"\b(entry|entries|targets?|tp\s?\d?|take[- ]profit|stop[- ]?loss|sl)\b", re.IGNORECASE)
SIGNAL_STRENGTH = 0.7

# Adverts and "target hit" recaps from signal channels say nothing about the
# market; they would only add fake bullishness.
_PROMO = re.compile(r"\b(vip|join|subscribe|membership|premium|promo|discount|giveaway|"
                    r"referral|sign ?up|deposit|free lifetime)\b", re.IGNORECASE)
_RECAP = re.compile(r"\b(target|tp\s?\d?)\s*(\d\s*)?(hit|reached|done|achieved|smashed)\b|"
                    r"\bprofit\b.*\d+\s?%|\d+\s?%\s*profit", re.IGNORECASE)


def is_trade_call(text: str) -> bool:
    return bool(_LEVELS.search(text) and (_LONG.search(text) or _SHORT.search(text)))


def is_noise(text: str) -> bool:
    """Adverts and result recaps, unless the post also carries a trade call."""
    if is_trade_call(text) and not _RECAP.search(text):
        return False
    return bool(_PROMO.search(text) or _RECAP.search(text))


def score_text(text: str) -> float:
    """Sentiment from -1 (very bearish) to +1 (very bullish).

    Trade calls ("LONG $SOL, entry 140, targets 150/160, stop 132") are mostly
    numbers, which VADER reads as neutral, so their direction is scored directly.
    """
    if is_trade_call(text):
        longs, shorts = len(_LONG.findall(text)), len(_SHORT.findall(text))
        if longs != shorts:
            return SIGNAL_STRENGTH if longs > shorts else -SIGNAL_STRENGTH
    return _analyzer.polarity_scores(text)["compound"]


def coin_patterns(watchlist: dict[str, list[str]]) -> dict[str, re.Pattern]:
    pats = {}
    for coin, aliases in watchlist.items():
        words = [re.escape(a) for a in aliases or []]
        c = re.escape(coin)
        # $SOL, #SOL, SOL/USDT, SOLUSDT, plus the plain-word aliases.
        alts = [rf"[$#]{c}(?:/?USDT)?\b", rf"\b{c}/?USDT\b"] + [rf"\b{w}\b" for w in words]
        pats[coin] = re.compile("|".join(alts), re.IGNORECASE)
    return pats


def mentioned_coins(text: str, patterns: dict[str, re.Pattern]) -> list[str]:
    return [coin for coin, pat in patterns.items() if pat.search(text)]


def _empty():
    return {"sum_w": 0.0, "sum_ws": 0.0, "mentions": 0, "bullish": 0, "bearish": 0, "posts": []}


def _finish(acc: dict, top_n: int) -> dict:
    n = acc["mentions"]
    raw = acc["sum_ws"] / acc["sum_w"] if acc["sum_w"] else 0.0
    acc["posts"].sort(key=lambda p: p["weight"] * abs(p["sentiment"]), reverse=True)
    return {
        "score": round(raw, 3),
        # Shrink towards neutral when only a few posts mention the coin.
        "adjusted": round(raw * n / (n + 5), 3),
        "mentions": n,
        "bullish_pct": round(100 * acc["bullish"] / n) if n else None,
        "bearish_pct": round(100 * acc["bearish"] / n) if n else None,
        "top_posts": acc["posts"][:top_n],
    }


def aggregate(posts: list[Post], watchlist: dict, weights: dict,
              now: datetime | None = None, half_life_hours: float = 12.0,
              top_n: int = 3, market_top_n: int = 12,
              ai_labels: dict[int, dict] | None = None) -> tuple[dict[str, dict], dict]:
    """Per-coin sentiment plus overall market mood from every post.

    With `ai_labels` (from bot.ai), the model's reading of each post replaces
    word scoring; posts the model skipped still fall back to word scoring.
    """
    now = now or datetime.now(timezone.utc)
    patterns = coin_patterns(watchlist)
    per_coin = {c: _empty() for c in watchlist}
    market = _empty()
    for i, post in enumerate(posts):
        label = (ai_labels or {}).get(i)
        if label:
            if label.get("advert"):
                continue
            s = stance_score(label)
            coins = [c for c in dict.fromkeys(x.upper() for x in label.get("coins", [])) if c in per_coin]
        else:
            if is_noise(post.text):
                continue
            s = score_text(post.text)
            coins = mentioned_coins(post.text, patterns)
        age_h = max((now - post.ts).total_seconds() / 3600, 0.0)
        w = (weights.get(post.source, 1.0) * post.source_weight * influence(post)
             * 0.5 ** (age_h / half_life_hours))
        entry = {**post.to_dict(), "sentiment": round(s, 3), "weight": round(w, 3), "coins": coins,
                 "read_by": "ai" if label else "words"}
        entry["text"] = entry["text"][:280]
        targets = [per_coin[c] for c in coins] + [market]
        for acc in targets:
            acc["sum_w"] += w
            acc["sum_ws"] += w * s
            acc["mentions"] += 1
            acc["bullish"] += s >= 0.25
            acc["bearish"] += s <= -0.25
            acc["posts"].append(entry)
    return {c: _finish(a, top_n) for c, a in per_coin.items()}, _finish(market, market_top_n)
