"""Optional AI reading of posts: Google's Gemini API (free tier) or any
OpenAI-compatible API such as Zhipu's free GLM-4.7-Flash.

Word counting can't tell an advert from a call, or "fear" in a Fear & Greed
update from fear about a coin. The model reads each post and returns which
coins it is about, the stance, how confident the author sounds, and whether
it is an advert. Without GEMINI_API_KEY, or if the call fails, the bot falls
back to word scoring.
"""
import json
import logging
import os

import requests

from .social import Post

log = logging.getLogger(__name__)
API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
BATCH = 40
MAX_CHARS = 700

PROMPT = """You label crypto social media posts for a market-sentiment tool.
For each post return:
- i: the post number
- coins: tickers (uppercase, e.g. BTC, ETH, SOL) the post expresses a view on. Leave out coins only mentioned in passing, and never infer a coin from words like "ton", "link" or "dot" used in their plain English sense.
- stance: "bullish", "bearish" or "neutral" about those coins (or about the market if no coin)
- confidence: 0 to 1, how strong and specific the view is (a trade call with levels is high; vague chatter is low)
- advert: true if the post mainly sells something (VIP groups, paid signals, broker sign-ups, giveaways) or only brags about past profits, rather than giving a view.
Judge the author's view of price direction, not the mood of the words: "short BTC" is bearish, "buy the fear" is bullish, a Fear & Greed index update with no view is neutral.

Posts:
"""

SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "i": {"type": "INTEGER"},
            "coins": {"type": "ARRAY", "items": {"type": "STRING"}},
            "stance": {"type": "STRING", "enum": ["bullish", "bearish", "neutral"]},
            "confidence": {"type": "NUMBER"},
            "advert": {"type": "BOOLEAN"},
        },
        "required": ["i", "coins", "stance", "confidence", "advert"],
    },
}


def _label_batch(posts: list[Post], offset: int, key: str, model: str) -> dict[int, dict]:
    lines = [f"[{offset + n}] ({p.source}, {p.author}) {p.text[:MAX_CHARS]}" for n, p in enumerate(posts)]
    body = {
        "contents": [{"parts": [{"text": PROMPT + "\n\n".join(lines)}]}],
        "generationConfig": {"responseMimeType": "application/json",
                             "responseSchema": SCHEMA, "temperature": 0},
    }
    r = requests.post(API.format(model=model), headers={"x-goog-api-key": key}, json=body, timeout=90)
    r.raise_for_status()
    text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
    return _parse_labels(text)


def _parse_labels(text: str) -> dict[int, dict]:
    """Accepts a bare JSON array or an object wrapping one, as models vary."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    data = json.loads(text)
    if isinstance(data, dict):
        data = next((v for v in data.values() if isinstance(v, list)), [])
    return {int(x["i"]): x for x in data if isinstance(x, dict) and "i" in x}


def _label_batch_openai(posts: list[Post], offset: int, key: str, model: str, base_url: str) -> dict[int, dict]:
    """Any OpenAI-compatible chat API, e.g. Zhipu GLM-4.7-Flash or Groq."""
    lines = [f"[{offset + n}] ({p.source}, {p.author}) {p.text[:MAX_CHARS]}" for n, p in enumerate(posts)]
    prompt = (PROMPT + "\n\n".join(lines)
              + '\n\nReply with only JSON: {"posts": [{"i": 0, "coins": ["BTC"], "stance": "bullish", '
              '"confidence": 0.8, "advert": false}, ...]}')
    r = requests.post(base_url.rstrip("/") + "/chat/completions",
                      headers={"Authorization": f"Bearer {key}"},
                      json={"model": model, "temperature": 0,
                            "messages": [{"role": "user", "content": prompt}]},
                      timeout=120)
    r.raise_for_status()
    return _parse_labels(r.json()["choices"][0]["message"]["content"])


def label_posts(posts: list[Post], model: str, provider: str = "gemini",
                base_url: str = "") -> tuple[dict[int, dict] | None, str]:
    """Labels keyed by post index, plus a status line for the dashboard."""
    key_name = "GEMINI_API_KEY" if provider == "gemini" else "AI_API_KEY"
    key = os.environ.get(key_name)
    if not key:
        return None, f"off (no {key_name}; using word scoring)"
    labels: dict[int, dict] = {}
    try:
        for start in range(0, len(posts), BATCH):
            batch = posts[start:start + BATCH]
            if provider == "gemini":
                labels.update(_label_batch(batch, start, key, model))
            else:
                labels.update(_label_batch_openai(batch, start, key, model, base_url))
    except Exception as e:  # noqa: BLE001 - fall back to word scoring
        log.warning("AI labelling failed, using word scoring: %s", e)
        return None, f"failed ({type(e).__name__}); using word scoring"
    return labels, f"ok ({len(labels)} posts read by {model})"


def stance_score(label: dict) -> float:
    """Label -> sentiment on the same -1..+1 scale as word scoring."""
    sign = {"bullish": 1.0, "bearish": -1.0}.get(label.get("stance"), 0.0)
    conf = min(max(float(label.get("confidence", 0.5)), 0.0), 1.0)
    return sign * (0.3 + 0.7 * conf)
