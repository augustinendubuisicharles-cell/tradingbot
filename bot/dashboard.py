"""Renders the static dashboard page (site/index.html) and its JSON data."""
from datetime import datetime, timedelta

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .alerts import fmt_price
from .config import SITE_DIR, TEMPLATE_DIR
from .storage import save_json


def safe_url(url: str) -> str:
    """Only plain web links from scraped posts become clickable."""
    return url if isinstance(url, str) and url.startswith(("https://", "http://")) else ""


def render(report: dict, history: list[dict], record: dict, out_dir=SITE_DIR) -> str:
    env = Environment(loader=FileSystemLoader(TEMPLATE_DIR),
                      autoescape=select_autoescape(["html", "j2"]))
    env.filters["safe_url"] = safe_url
    generated = datetime.fromisoformat(report["generated_at"])
    page = env.get_template("dashboard.html.j2").render(
        exchange=report["exchange"],
        generated_label=f"{generated:%d %b %H:%M}",
        next_label=f"{generated + timedelta(hours=4):%H:%M}",
        fng=report.get("fear_greed"),
        mood=report["market_mood"],
        lookback=report["lookback_hours"],
        suggestions=report["suggestions"],
        coins=sorted(report["coins"], key=lambda a: a["score"], reverse=True),
        top_posts=report["market_mood"]["top_posts"],
        history=list(reversed(history))[:20],
        record=record,
        health=report["source_health"],
        trending=report.get("trending", []),
        fp=fmt_price,
        demo=report.get("demo", False),
        emerging=report.get("emerging"),
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "index.html").write_text(page)
    save_json(out_dir / "data.json", report)
    return page
