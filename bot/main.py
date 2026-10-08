"""Entry point.

  python -m bot.main run --publish --digest   # 4-hourly: analyse, update dashboard, send digest
  python -m bot.main run --alerts             # frequent: analyse and send alerts only
  python -m bot.main demo                     # offline preview with made-up data -> demo/index.html
  python -m bot.main telegram-chat-id         # find your chat id after messaging your bot
"""
import argparse
import logging
import os
import uuid
from datetime import datetime, timezone

from . import ai, alerts, analysis, dashboard, emerging, market, social, trend
from .config import DATA_DIR, ROOT, load_config
from .sentiment import aggregate
from .storage import load_json, load_jsonl, save_json, save_jsonl

log = logging.getLogger("bot")


def build_report(cfg: dict, mkt, posts, health: dict, fng, trending, state: dict,
                 now: datetime, ai_labels: dict | None = None) -> tuple[dict, dict]:
    coins = list(cfg["watchlist"])
    try:
        tickers = mkt.tickers(coins)
        missing = [c for c in coins if c not in tickers]
        health["exchange"] = f"ok ({len(tickers)} markets)" + (
            f"; not listed: {', '.join(missing)}" if missing else "")
    except Exception as e:  # noqa: BLE001
        log.error("exchange tickers failed: %s", e)
        health["exchange"] = f"failed: {type(e).__name__}"
        tickers = {}

    sentiments, mood = aggregate(posts, cfg["watchlist"], cfg["sources"]["weights"], now=now,
                                 ai_labels=ai_labels)
    prev_oi = state.get("oi", {})
    analyses, candles = [], {}
    for coin in coins:
        if coin not in tickers:
            continue
        try:
            c4h = mkt.candles(coin, cfg["timeframe"], 200)
            c1d = mkt.candles(coin, "1d", 120)
            fut = mkt.futures_signals(coin)
        except Exception as e:  # noqa: BLE001
            log.error("market data failed for %s: %s", coin, e)
            health[f"exchange:{coin}"] = f"failed: {type(e).__name__}"
            continue
        analyses.append(analysis.analyze_coin(
            coin, tickers[coin], c4h, c1d, fut, sentiments[coin], fng, prev_oi.get(coin), cfg))
        candles[coin] = c4h

    report = {
        "generated_at": now.isoformat(),
        "exchange": cfg["exchange"],
        "lookback_hours": cfg["sources"]["lookback_hours"],
        "fear_greed": fng,
        "trending": trending,
        "market_mood": mood,
        "coins": analyses,
        "suggestions": analysis.rank(analyses, cfg["risk"]["max_suggestions"]),
        "source_health": health,
    }
    return report, candles


def log_new_suggestions(history: list[dict], report: dict, now: datetime) -> None:
    active = {h["coin"] for h in history if h["status"] in ("open", "pending")}
    for a in report["suggestions"]:
        if a["coin"] in active:
            continue
        s = a["suggestion"]
        history.append({
            "id": uuid.uuid4().hex[:8], "coin": a["coin"], "created": now.isoformat(),
            "entry": s["entry"], "stop": s["stop"], "tp1": s["tp1"], "tp2": s["tp2"],
            "score": a["score"], "status": "pending" if s["order"].startswith("limit") else "open",
        })


def publish(report: dict, candles: dict, state: dict, now: datetime,
            data_dir=DATA_DIR, site_dir=None, persist: bool = True) -> dict:
    history = load_jsonl(data_dir / "history.jsonl") if persist else []
    analysis.evaluate_open(history, candles, now)
    log_new_suggestions(history, report, now)
    record = analysis.summarize_record(history)
    report["record"] = record
    if persist:
        report["backtest"] = load_json(data_dir / "backtest.json", None)
        save_jsonl(data_dir / "history.jsonl", history)
        save_json(data_dir / "latest.json", report)
        state["oi"] = {a["coin"]: a["open_interest"] for a in report["coins"] if a["open_interest"]}
        save_json(data_dir / "state.json", state)
    kwargs = {"out_dir": site_dir} if site_dir else {}
    dashboard.render(report, history, record, **kwargs)
    return record


def run_emerging(cfg: dict, mkt, posts, report: dict, state: dict, morning: bool) -> list[str]:
    """Morning: full hidden-gem scan and the morning report. Other runs: re-time this
    morning's picks with fresh prices. A failure never blocks the main report."""
    texts, trending = [p.text for p in posts], report.get("trending", [])
    path = DATA_DIR / "emerging.json"
    scan = load_json(path, {})
    health = report["source_health"]
    msgs = []
    try:
        if morning:
            tokens, scan = emerging.morning_scan(cfg, mkt, texts, trending)
            save_json(path, scan)
            msgs += emerging.morning_messages(tokens, scan["hot_narratives"], cfg["emerging"]["report_size"],
                                              os.environ.get("DASHBOARD_URL"))
        elif scan:
            tokens = emerging.rerate(scan, mkt, cfg, texts, trending)
        else:
            health["hidden gems"] = "waiting for the first morning scan"
            return []
    except Exception as e:  # noqa: BLE001
        log.error("hidden-gem scan failed: %s", e)
        health["hidden gems"] = f"failed: {type(e).__name__}" + ("; showing the last scan" if scan else "")
        if not scan:
            return []
        try:
            tokens = emerging.rerate(scan, mkt, cfg, texts, trending)
        except Exception:  # noqa: BLE001
            return []
    else:
        health["hidden gems"] = (f"ok ({len(tokens)} checked, {sum(t['safe'] for t in tokens)} passed safety; "
                                 f"scan from {scan['as_of'][:16].replace('T', ' ')} UTC)")
    report["emerging"] = {"tokens": tokens, "hot": scan.get("hot_narratives", []), "as_of": scan.get("as_of")}
    track_picks(cfg, mkt, tokens, report)
    return msgs + emerging.status_alerts(tokens, state)


def run_trend(cfg: dict, mkt, report: dict, now: datetime) -> list[str]:
    """BTC/ETH trend signals: refresh daily prices, work out target positions,
    paper-trade them from the go-live day, and message any change."""
    health = report["source_health"]
    hist_path, state_path = DATA_DIR / "trend_daily.json", DATA_DIR / "trend_state.json"
    hist, state = load_json(hist_path, {}), load_json(state_path, {})
    try:
        hist = trend.update_history(hist, mkt, now)
        save_json(hist_path, hist)
    except Exception as e:  # noqa: BLE001
        log.error("trend prices failed: %s", e)
        health["trend signals"] = f"prices failed: {type(e).__name__}; using saved prices"
    w = trend.weights(hist)
    if not w:
        return []
    account = float(cfg["risk"]["account_size"])
    first = "start" not in state
    state.setdefault("start", max(d["as_of"] for d in w.values()))
    msgs = trend.signals(w, state, account)
    if first:
        msgs.append(trend.intro_text(w, account))
    live = trend.simulate(hist, w, start=state["start"])
    save_json(state_path, state)
    health.setdefault("trend signals", f"ok (prices to {max(d['as_of'] for d in w.values())})")
    report["trend"] = trend_view(hist, w, live, state["start"], account)
    return msgs


def trend_view(hist: dict, w: dict, live: dict, start: str, account: float) -> dict:
    return {
        "account": account,
        "coins": {c: {k: v for k, v in d.items() if k not in ("dates", "series")} |
                  {"spark": [r[2] for r in hist[c][-90:]]} for c, d in w.items()},
        "total": sum(d["target"] for d in w.values()),
        "live": trend.stats(live["dates"], live["equity"]) | {"start": start},
        "tested": load_json(DATA_DIR / "trend_backtest.json", None),
    }


def track_picks(cfg: dict, mkt, tokens: list[dict], report: dict) -> None:
    """Settles earlier gem picks against real prices and logs today's new ones."""
    path = DATA_DIR / "gem_history.jsonl"
    history = load_jsonl(path)
    candles = {}
    for sym in {h["symbol"] for h in history if h["status"] in ("open", "pending")}:
        try:
            candles[sym] = mkt.candles(sym, "4h", 200)
        except Exception as e:  # noqa: BLE001
            log.warning("could not settle %s: %s", sym, e)
    emerging.settle_picks(history, candles)
    emerging.log_picks(history, tokens, datetime.now(timezone.utc), emerging.timing_params(cfg))
    save_jsonl(path, history)
    report["emerging"]["record"] = emerging.picks_record(history)
    report["emerging"]["history"] = list(reversed(history))[:20]


def apply_tuning(cfg: dict) -> dict:
    """Uses the backtest's timing settings when it adopted better ones."""
    tuned = load_json(DATA_DIR / "tuned.json", {})
    if tuned.get("adopted"):
        cfg.setdefault("emerging", {})["timing"] = tuned["params"]
    return cfg


def cmd_run(args) -> None:
    cfg = apply_tuning(load_config())
    now = datetime.now(timezone.utc)
    state = load_json(DATA_DIR / "state.json", {})
    mkt = market.Market(cfg["exchange"], cfg["quote"])
    posts, health = social.collect(cfg, now)
    labels = None
    # AI reading runs on the 4-hourly publish only (it's the slow part, and
    # keeps paid-API options inside free quotas); 30-minute alert checks use
    # word scoring.
    if args.publish and cfg.get("ai", {}).get("enabled", True):
        acfg = cfg.get("ai", {})
        if acfg.get("provider", "local") == "local":
            from . import local_model
            labels, health["ai post reading"] = local_model.label_posts(posts, cfg["watchlist"])
        else:
            labels, health["ai post reading"] = ai.label_posts(
                posts, acfg.get("model", ""), acfg["provider"], acfg.get("base_url", ""))
    report, candles = build_report(cfg, mkt, posts, health, market.fear_greed(),
                                   market.trending_coins(), state, now, ai_labels=labels)
    log.info("scored %d coins, %d ideas, %d posts", len(report["coins"]),
             len(report["suggestions"]), len(posts))
    if not report["coins"]:
        # Keep the last good dashboard rather than publishing an empty one, and
        # fail the run so GitHub emails a notice.
        raise SystemExit("no market data from the exchange; nothing published")

    if args.publish:
        token_msgs = []
        if cfg.get("trend", {}).get("enabled", True):
            token_msgs += run_trend(cfg, mkt, report, now)
        if cfg.get("emerging", {}).get("enabled"):
            token_msgs += run_emerging(cfg, mkt, posts, report, state, morning=args.morning)
        publish(report, candles, state, now)
        if args.digest:
            alerts.send_telegram(alerts.digest_text(report, os.environ.get("DASHBOARD_URL")))
        for msg in token_msgs:
            alerts.send_telegram(msg)

    if args.alerts:
        astate = load_json(DATA_DIR / "alert_state.json", {})
        for msg in alerts.check_alerts(report, astate, cfg, now):
            alerts.send_telegram(msg)
        save_json(DATA_DIR / "alert_state.json", astate)


def cmd_demo(_args) -> None:
    from .demo import FakeMarket, fake_posts

    cfg = load_config()
    now = datetime.now(timezone.utc)
    posts = fake_posts(now)
    health = {"demo": "made-up data for preview only"}
    report, candles = build_report(cfg, FakeMarket(now), posts, health,
                                   {"value": 58, "label": "Greed"},
                                   ["SUI", "SOL", "PEPE"], {}, now)
    report["demo"] = True
    from .demo import fake_emerging
    scan = fake_emerging()
    tokens = emerging.rerate(scan, FakeMarket(now), cfg, [p.text for p in posts], ["FET"])
    report["emerging"] = {"tokens": tokens, "hot": scan["hot_narratives"], "as_of": scan["as_of"]}
    print("\n\n".join(emerging.morning_messages(tokens, scan["hot_narratives"], 5, None)) + "\n")
    report["backtest"] = load_json(DATA_DIR / "backtest.json", None)
    hist = load_json(DATA_DIR / "trend_daily.json", {})
    w = trend.weights(hist)
    if w:
        start = sorted(hist["BTC"])[-30][0]
        report["trend"] = trend_view(hist, w, trend.simulate(hist, w, start=start), start, 1000.0)
        print(trend.intro_text(w, 1000) + "\n")
    publish(report, candles, {}, now, site_dir=ROOT / "demo", persist=False)
    print(alerts.digest_text(report, "https://example.pages.dev"))
    print(f"\nDemo dashboard written to {ROOT / 'demo' / 'index.html'}")


def cmd_backtest(_args) -> None:
    from . import backtest

    cfg = load_config()
    mkt = market.Market(cfg["exchange"], cfg["quote"])
    extra = list(cfg["watchlist"]) + [b["symbol"] for b in load_json(DATA_DIR / "emerging.json", {}).get("bases", [])]
    report, tuned = backtest.run(cfg, mkt, extra)
    save_json(DATA_DIR / "backtest.json", report)
    save_json(DATA_DIR / "tuned.json", tuned)
    log.info("backtest: %d coins, %d candles, %ss; adopted=%s (%s)", report["coins"], report["candles"],
             report["seconds"], tuned["adopted"], tuned["reason"])
    alerts.send_telegram(backtest.summary_text(report))


def cmd_chat_id(_args) -> None:
    chats = alerts.telegram_chat_ids()
    if not chats:
        print("No messages found. Send any message to your bot in Telegram, then run this again.")
    for chat_id, name in chats:
        print(f"{chat_id}\t{name}")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="bot")
    sub = p.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run")
    run.add_argument("--publish", action="store_true", help="update dashboard and track record")
    run.add_argument("--digest", action="store_true", help="send the 4h Telegram summary")
    run.add_argument("--alerts", action="store_true", help="send real-time Telegram alerts")
    run.add_argument("--morning", action="store_true", help="full hidden-gem scan and morning report")
    run.set_defaults(fn=cmd_run)
    sub.add_parser("demo").set_defaults(fn=cmd_demo)
    sub.add_parser("telegram-chat-id").set_defaults(fn=cmd_chat_id)
    sub.add_parser("backtest").set_defaults(fn=cmd_backtest)
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
