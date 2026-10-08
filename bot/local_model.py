"""Free AI post reading that runs inside the bot: no account, key or bill.

Uses CryptoBERT (MIT licence), a model trained on millions of crypto social
posts to label them Bullish, Neutral or Bearish. It understands crypto slang
and context far better than word counting. It doesn't spot adverts or pick
out coins, so those still use the bot's own rules.
"""
import logging

from .sentiment import SIGNAL_STRENGTH, coin_patterns, is_noise, is_trade_call, mentioned_coins, score_text
from .social import Post

log = logging.getLogger(__name__)
MODEL = "ElKulako/cryptobert"
STANCE = {"bullish": "bullish", "bearish": "bearish", "neutral": "neutral"}

_pipe = None


def _pipeline():
    global _pipe
    if _pipe is None:
        from transformers import pipeline  # heavy import, only on 4-hourly runs

        _pipe = pipeline("text-classification", model=MODEL, truncation=True, max_length=128)
    return _pipe


def label_posts(posts: list[Post], watchlist: dict, classify=None) -> tuple[dict[int, dict] | None, str]:
    """Labels in the same shape as bot.ai, so scoring treats them alike."""
    try:
        classify = classify or _pipeline()
        outputs = classify([p.text[:1000] for p in posts], batch_size=16) if posts else []
    except Exception as e:  # noqa: BLE001 - fall back to word scoring
        log.warning("local model unavailable, using word scoring: %s", e)
        return None, f"failed ({type(e).__name__}); using word scoring"

    patterns = coin_patterns(watchlist)
    labels = {}
    for i, (post, out) in enumerate(zip(posts, outputs)):
        stance = STANCE.get(str(out["label"]).lower(), "neutral")
        confidence = float(out["score"])
        if is_trade_call(post.text):
            # An explicit LONG/SHORT with levels beats the model's guess.
            s = score_text(post.text)
            if abs(s) == SIGNAL_STRENGTH:
                stance, confidence = ("bullish" if s > 0 else "bearish"), 0.9
        labels[i] = {"i": i, "coins": mentioned_coins(post.text, patterns), "stance": stance,
                     "confidence": confidence, "advert": is_noise(post.text)}
    return labels, f"ok ({len(labels)} posts read by CryptoBERT)"
