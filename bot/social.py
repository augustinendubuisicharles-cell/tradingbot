"""Free social and news sources: public Telegram channels, Reddit, news RSS.

Each collector returns [] (and records the error) when its source is down or
blocked, so one bad source never stops the run.
"""
import calendar
import logging
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

import feedparser
import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)
HTTP_TIMEOUT = 20
HEADERS = {"User-Agent": "tradingbot/0.1 (personal market research)"}


@dataclass
class Post:
    source: str        # telegram | reddit | news
    author: str        # channel, subreddit or publication
    text: str
    ts: datetime
    url: str
    engagement: float = 0.0  # views, upvotes; 0 when unknown
    source_weight: float = 1.0  # trust in this particular channel (config)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["ts"] = self.ts.isoformat()
        return d


def _parse_count(s: str) -> float:
    """'12.3K' -> 12300.0"""
    s = s.strip().upper()
    mult = {"K": 1e3, "M": 1e6}.get(s[-1:], 1)
    try:
        return float(s.rstrip("KM")) * mult
    except ValueError:
        return 0.0


def parse_telegram_page(channel: str, html: str) -> list[Post]:
    soup = BeautifulSoup(html, "html.parser")
    posts = []
    for msg in soup.select("div.tgme_widget_message"):
        text_el = msg.select_one("div.tgme_widget_message_text")
        time_el = msg.select_one("a.tgme_widget_message_date time")
        if not text_el or not time_el or not time_el.get("datetime"):
            continue
        views_el = msg.select_one("span.tgme_widget_message_views")
        post_id = msg.get("data-post", channel)
        posts.append(Post(
            source="telegram",
            author=channel,
            text=text_el.get_text(" ", strip=True),
            ts=datetime.fromisoformat(time_el["datetime"]).astimezone(timezone.utc),
            url=f"https://t.me/{post_id}",
            engagement=_parse_count(views_el.get_text()) if views_el else 0.0,
        ))
    return posts


def telegram_channel(channel: str) -> list[Post]:
    r = requests.get(f"https://t.me/s/{channel}", headers=HEADERS, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return parse_telegram_page(channel, r.text)


def parse_reddit_listing(subreddit: str, data: dict) -> list[Post]:
    posts = []
    for child in data.get("data", {}).get("children", []):
        p = child.get("data", {})
        text = " ".join(x for x in (p.get("title"), p.get("selftext")) if x)
        if not text:
            continue
        posts.append(Post(
            source="reddit",
            author=f"r/{subreddit}",
            text=text[:2000],
            ts=datetime.fromtimestamp(p.get("created_utc", 0), tz=timezone.utc),
            url="https://www.reddit.com" + p.get("permalink", ""),
            engagement=float(p.get("score", 0)) + float(p.get("num_comments", 0)),
        ))
    return posts


def subreddit(name: str) -> list[Post]:
    r = requests.get(f"https://www.reddit.com/r/{name}/new.json?limit=100",
                     headers=HEADERS, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return parse_reddit_listing(name, r.json())


def parse_feed(content: bytes) -> list[Post]:
    feed = feedparser.parse(content)
    publisher = feed.feed.get("title", "news")
    posts = []
    for e in feed.entries:
        when = e.get("published_parsed") or e.get("updated_parsed")
        if not when:
            continue
        summary = BeautifulSoup(e.get("summary", ""), "html.parser").get_text(" ", strip=True)
        posts.append(Post(
            source="news",
            author=publisher,
            text=f"{e.get('title', '')}. {summary}"[:2000],
            ts=datetime.fromtimestamp(calendar.timegm(when), tz=timezone.utc),
            url=e.get("link", ""),
        ))
    return posts


def rss(url: str) -> list[Post]:
    r = requests.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return parse_feed(r.content)


def collect(cfg: dict, now: datetime | None = None) -> tuple[list[Post], dict[str, str]]:
    """All posts from the last `lookback_hours`, plus a per-source health map."""
    src = cfg["sources"]
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=src.get("lookback_hours", 24))
    channels = [c if isinstance(c, dict) else {"name": c} for c in src.get("telegram_channels") or []]
    channel_weight = {c["name"]: float(c.get("weight", 1.0)) for c in channels}
    jobs = (
        [(f"telegram:{c['name']}", telegram_channel, c["name"]) for c in channels]
        + [(f"reddit:{s}", subreddit, s) for s in src.get("subreddits") or []]
        + [(f"news:{u}", rss, u) for u in src.get("rss_feeds") or []]
    )
    posts, health = [], {}
    for name, fn, arg in jobs:
        try:
            got = [p for p in fn(arg) if p.ts >= cutoff]
            for p in got:
                p.source_weight = channel_weight.get(p.author, 1.0) if p.source == "telegram" else 1.0
            posts.extend(got)
            health[name] = f"ok ({len(got)} posts)"
        except Exception as e:  # noqa: BLE001
            log.warning("source %s failed: %s", name, e)
            health[name] = f"failed: {type(e).__name__}"
    return posts, health


def influence(post: Post) -> float:
    """Rough weight for how much attention a post got (1.0 when unknown)."""
    return 1.0 + math.log10(1.0 + max(post.engagement, 0.0)) / 2
