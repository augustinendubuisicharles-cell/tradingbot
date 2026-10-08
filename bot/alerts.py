"""Telegram alerts: strong signals, sharp price moves, sentiment swings, 4h digest."""
import html
import logging
import os
from datetime import datetime, timedelta, timezone

import requests

log = logging.getLogger(__name__)


def fmt_price(p: float | None) -> str:
    if p is None:
        return "-"
    if p >= 1000:
        return f"{p:,.0f}"
    if p >= 1:
        return f"{p:,.2f}"
    return f"{p:.5f}"


def send_telegram(text: str) -> bool:
    token, chat_id = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        log.warning("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set; printing instead:\n%s", text)
        return False
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": True},
        timeout=20,
    )
    if not r.ok:
        log.error("telegram send failed: %s %s", r.status_code, r.text[:200])
    return r.ok


def telegram_chat_ids() -> list[tuple[str, str]]:
    """Chats that have messaged the bot recently: (chat id, name)."""
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=20)
    r.raise_for_status()
    seen = {}
    for u in r.json().get("result", []):
        chat = (u.get("message") or u.get("channel_post") or {}).get("chat")
        if chat:
            seen[str(chat["id"])] = chat.get("username") or chat.get("title") or chat.get("first_name", "")
    return list(seen.items())


def suggestion_text(a: dict) -> str:
    s = a["suggestion"]
    reasons = "; ".join(s["reasons"])
    return (
        f"<b>{a['coin']}</b> {s['side']} · score {a['score']:.0f} ({s['confidence']})\n"
        f"Entry {fmt_price(s['entry'])}, {s['order']} order\n"
        f"Stop {fmt_price(s['stop'])} · TP1 {fmt_price(s['tp1'])} · TP2 {fmt_price(s['tp2'])}\n"
        f"Size ≈ {s['notional']:,.0f} USDT, max loss ≈ {s['max_loss']:,.0f} USDT\n"
        f"<i>{html.escape(reasons)}</i>"
    )


def check_alerts(report: dict, state: dict, cfg: dict, now: datetime | None = None) -> list[str]:
    """New alert messages, honouring cooldowns. Updates `state` in place."""
    now = now or datetime.now(timezone.utc)
    acfg = cfg["alerts"]
    sent = state.setdefault("sent", {})
    baseline = state.setdefault("baseline", {})
    prev_sent = state.setdefault("sentiment", {})
    cooldown = timedelta(hours=acfg["cooldown_hours"])
    messages = []

    def fire(key: str, text: str):
        last = sent.get(key)
        if last and now - datetime.fromisoformat(last) < cooldown:
            return
        sent[key] = now.isoformat()
        messages.append(text)

    for a in report["coins"]:
        coin, price = a["coin"], a["price"]

        if a["suggestion"] and a["score"] >= acfg["strong_score"]:
            fire(f"signal:{coin}", "🟢 <b>Strong setup</b>\n" + suggestion_text(a))

        base = baseline.get(coin)
        if not base or now - datetime.fromisoformat(base["ts"]) > timedelta(hours=4):
            baseline[coin] = base = {"price": price, "ts": now.isoformat()}
        move = (price - base["price"]) / base["price"] * 100
        if abs(move) >= acfg["price_move_pct"]:
            arrow = "🚀" if move > 0 else "🔻"
            fire(f"move:{coin}:{'up' if move > 0 else 'down'}",
                 f"{arrow} <b>{coin}</b> moved {move:+.1f}% to {fmt_price(price)} since "
                 f"{datetime.fromisoformat(base['ts']):%H:%M} UTC")
            baseline[coin] = {"price": price, "ts": now.isoformat()}

        s = a["sentiment"]
        before = prev_sent.get(coin)
        if before is not None and s["mentions"] >= 5 and abs(s["adjusted"] - before) >= acfg["sentiment_shift"]:
            word = "turned bullish" if s["adjusted"] > before else "turned bearish"
            fire(f"sentiment:{coin}",
                 f"💬 <b>{coin}</b> sentiment {word}: {before:+.2f} → {s['adjusted']:+.2f} "
                 f"({s['mentions']} posts)")
        prev_sent[coin] = s["adjusted"]
    return messages


def digest_text(report: dict, dashboard_url: str | None) -> str:
    fng = report.get("fear_greed")
    mood = report["market_mood"]
    lines = [f"📊 <b>4h market update</b> · {report['generated_at'][:16].replace('T', ' ')} UTC"]
    if fng:
        lines.append(f"Fear &amp; Greed: {fng['value']} ({html.escape(fng['label'])})")
    lines.append(f"Social mood: {mood['score']:+.2f} across {mood['mentions']} posts")
    if report["suggestions"]:
        lines.append("\n<b>Spot ideas</b>")
        lines += [suggestion_text(a) + "\n" for a in report["suggestions"]]
    else:
        lines.append("\nNo low-risk spot setups right now. Sitting out is a position too.")
    top = sorted(report["coins"], key=lambda a: a["score"], reverse=True)[:5]
    lines.append("Top scores: " + ", ".join(f"{a['coin']} {a['score']:.0f}" for a in top))
    em = [t for t in report.get("emerging", {}).get("tokens", [])
          if t["plan"]["status"] in ("Enter zone", "Wait for pullback", "Watch for breakout")][:4]
    if em:
        lines.append("\n<b>Emerging AI / RWA tokens</b>")
        for t in em:
            lines.append(f"{t['symbol']} ({t['narrative']}, {t['risk_rating'].lower()} risk): "
                         f"{t['plan']['status'].lower()}, entry {fmt_price(t['plan']['entry'])}, "
                         f"stop {fmt_price(t['plan']['stop'])}")
    if dashboard_url:
        lines.append(f'\n<a href="{html.escape(dashboard_url)}">Open dashboard</a>')
    lines.append("<i>Not financial advice. Ideas only; the bot never trades.</i>")
    return "\n".join(lines)
