"""Renders the static dashboard page (site/index.html) and its JSON data."""
from datetime import datetime, timedelta

from markupsafe import Markup, escape

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .alerts import fmt_price
from .config import SITE_DIR, TEMPLATE_DIR
from .storage import save_json


def safe_url(url: str) -> str:
    """Only plain web links from scraped posts become clickable."""
    return url if isinstance(url, str) and url.startswith(("https://", "http://")) else ""


def spark_svg(closes: list[float] | None, plan: dict | None = None, span: str = "7 days") -> Markup:
    """A price line with the plan's buy, stop and target levels as labelled dashed lines."""
    if not closes or len(closes) < 2:
        return Markup("")
    plan = plan or {}
    levels = [(k, plan[k]) for k in ("add", "tp2", "tp1", "entry", "trim", "stop", "exit") if isinstance(plan.get(k), (int, float))]
    names = {"tp2": "T2", "tp1": "T1", "entry": "Buy", "stop": "Stop", "add": "Add", "trim": "Trim", "exit": "Exit"}
    colors = {"tp2": "var(--up)", "tp1": "var(--up)", "entry": "var(--ink-2)", "stop": "var(--down)",
              "add": "var(--up)", "trim": "var(--ink-2)", "exit": "var(--down)"}
    vals = list(closes) + [v for _, v in levels]
    lo, hi = min(vals), max(vals)
    pad = (hi - lo) * 0.08 or hi * 0.01 or 1
    lo, hi = lo - pad, hi + pad
    w, h, right = 320.0, 96.0, 36.0
    x = lambda i: 2 + i * (w - right - 4) / (len(closes) - 1)  # noqa: E731
    y = lambda v: h - 4 - (v - lo) * (h - 8) / (hi - lo)  # noqa: E731
    pts = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(closes))
    parts = [f'<svg class="spark" viewBox="0 0 {w:.0f} {h:.0f}" role="img" '
             f'aria-label="Price over the last {escape(span)} with plan levels">',
             f"<title>{escape(span)}: low {fmt_price(min(closes))}, high {fmt_price(max(closes))}, "
             f"now {fmt_price(closes[-1])}</title>"]
    last_label_y = -99.0
    for k, v in levels:
        yy = y(v)
        parts.append(f'<line x1="2" x2="{w - right:.0f}" y1="{yy:.1f}" y2="{yy:.1f}" stroke="{colors[k]}" '
                     f'stroke-width="1" stroke-dasharray="3 3"/>')
        ty = max(yy + 3.5, last_label_y + 10)
        last_label_y = ty
        parts.append(f'<text x="{w - right + 4:.0f}" y="{ty:.1f}" class="lvl">{escape(names[k])}</text>')
    parts.append(f'<polyline points="{pts}" fill="none" stroke="var(--bar)" stroke-width="2" '
                 f'stroke-linejoin="round" stroke-linecap="round"/>')
    parts.append(f'<circle cx="{x(len(closes) - 1):.1f}" cy="{y(closes[-1]):.1f}" r="3.5" fill="var(--bar)" '
                 f'stroke="var(--surface)" stroke-width="2"/>')
    parts.append("</svg>")
    return Markup("".join(parts))


def render(report: dict, history: list[dict], record: dict, out_dir=SITE_DIR) -> str:
    env = Environment(loader=FileSystemLoader(TEMPLATE_DIR),
                      autoescape=select_autoescape(["html", "j2"]))
    env.filters["safe_url"] = safe_url
    env.globals["spark"] = spark_svg
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
        trend=report.get("trend"),
        backtest=report.get("backtest"),
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "index.html").write_text(page)
    save_json(out_dir / "data.json", report)
    return page
