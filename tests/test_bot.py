from datetime import datetime, timedelta, timezone

from bot import alerts, analysis, sentiment, social
from bot.config import load_config

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
CFG = load_config()


def candle(ts_h, o, h, l, c):
    return [(NOW + timedelta(hours=ts_h)).timestamp() * 1000, o, h, l, c, 1.0]


def test_ema_and_rsi():
    assert analysis.ema([1, 2, 3, 4, 5], 3)[-1] == 4.0
    rising = [float(i) for i in range(1, 40)]
    assert analysis.rsi(rising) == 100.0
    falling = list(reversed(rising))
    assert analysis.rsi(falling) < 1


def test_sentiment_direction_and_coin_detection():
    assert sentiment.score_text("BTC breakout, very bullish 🚀") > 0.5
    assert sentiment.score_text("ETH dumping, got rekt, bearish") < -0.5
    pats = sentiment.coin_patterns(CFG["watchlist"])
    assert sentiment.mentioned_coins("Solana and $LINK look strong", pats) == ["SOL", "LINK"]
    # Plain English words must not count as coin mentions.
    assert sentiment.mentioned_coins("a ton of people clicked the link", pats) == []


def _analysis(score=80, trend=100, rsi=55, price=100.0, atr=2.0, ema20=99.0):
    return {"eligible": True, "atr": atr, "score": score, "rsi": rsi, "price": price,
            "ema20": ema20, "components": {"trend": trend}, "trend_notes": [],
            "sentiment": {"mentions": 0}, "funding_rate": None, "atr_pct": 2.0}


def test_suggestion_risk_maths():
    s = analysis.suggest(_analysis(), CFG["risk"])
    assert s["stop"] == 96.0 and s["tp1"] == 106.0 and s["tp2"] == 112.0
    # 1% of 1000 = 10 USDT risk over a 4 USDT stop distance = 2.5 units = 250 USDT,
    # which is exactly the 25% position cap.
    assert round(s["max_loss"], 6) == 10.0
    assert round(s["notional"], 6) == 250.0


def test_no_suggestion_when_overbought_downtrend_or_weak():
    r = CFG["risk"]
    assert analysis.suggest(_analysis(rsi=78), r) is None
    assert analysis.suggest(_analysis(trend=30), r) is None
    assert analysis.suggest(_analysis(score=50), r) is None


def test_stretched_price_waits_for_pullback():
    s = analysis.suggest(_analysis(price=110.0, ema20=100.0), CFG["risk"])
    assert s["order"].startswith("limit") and s["entry"] == 100.0


def test_track_record_settles_stop_first():
    h = [{"coin": "X", "created": NOW.isoformat(), "entry": 100, "stop": 96, "tp1": 106,
          "status": "open"}]
    # One candle spans both stop and target: counted as a loss.
    analysis.evaluate_open(h, {"X": [candle(4, 100, 107, 95, 101)]}, NOW + timedelta(hours=8))
    assert h[0]["status"] == "stopped" and h[0]["result_r"] == -1.0

    h = [{"coin": "X", "created": NOW.isoformat(), "entry": 100, "stop": 96, "tp1": 106,
          "status": "pending"}]
    analysis.evaluate_open(h, {"X": [candle(4, 103, 108, 102, 107)]}, NOW + timedelta(hours=8))
    assert h[0]["status"] == "pending"  # never traded down to the limit entry
    analysis.evaluate_open(h, {"X": [candle(4, 103, 108, 102, 107), candle(8, 101, 101, 99.5, 100),
                                     candle(12, 100, 106.5, 99.8, 106)]}, NOW + timedelta(hours=16))
    assert h[0]["status"] == "target hit"
    assert analysis.summarize_record(h)["win_rate"] == 100


def _report(price, adjusted=0.0, mentions=0, suggestion=None, score=50):
    return {"coins": [{"coin": "BTC", "price": price, "score": score, "suggestion": suggestion,
                       "sentiment": {"adjusted": adjusted, "mentions": mentions}}]}


def test_alerts_price_move_and_cooldown():
    state = {}
    assert alerts.check_alerts(_report(100), state, CFG, NOW) == []
    msgs = alerts.check_alerts(_report(106), state, CFG, NOW + timedelta(minutes=30))
    assert len(msgs) == 1 and "+6.0%" in msgs[0]
    # Same direction again inside the cooldown: silent.
    assert alerts.check_alerts(_report(112), state, CFG, NOW + timedelta(hours=1)) == []


def test_alerts_sentiment_shift():
    state = {}
    alerts.check_alerts(_report(100, adjusted=0.0, mentions=8), state, CFG, NOW)
    msgs = alerts.check_alerts(_report(100, adjusted=-0.5, mentions=8), state, CFG,
                               NOW + timedelta(minutes=30))
    assert len(msgs) == 1 and "bearish" in msgs[0]


TELEGRAM_HTML = """
<div class="tgme_widget_message" data-post="chan/42">
  <div class="tgme_widget_message_text">$SOL looks <b>bullish</b></div>
  <span class="tgme_widget_message_views">12.5K</span>
  <a class="tgme_widget_message_date"><time datetime="2026-10-08T10:00:00+00:00"></time></a>
</div>"""


def test_parse_telegram_and_reddit():
    [p] = social.parse_telegram_page("chan", TELEGRAM_HTML)
    assert p.text == "$SOL looks bullish" and p.engagement == 12500 and p.url == "https://t.me/chan/42"
    listing = {"data": {"children": [{"data": {"title": "BTC up", "selftext": "", "created_utc": 1.8e9,
                                               "permalink": "/r/x/1", "score": 10, "num_comments": 2}}]}}
    [r] = social.parse_reddit_listing("x", listing)
    assert r.text == "BTC up" and r.engagement == 12


def test_demo_pipeline_renders_and_escapes(tmp_path):
    from bot.demo import FakeMarket, fake_posts
    from bot.main import build_report, publish

    posts = fake_posts(NOW) + [social.Post("telegram", "evil", "<script>alert(1)</script> $BTC moon",
                                           NOW, "javascript:alert(1)", 99999)]
    report, candles = build_report(CFG, FakeMarket(NOW), posts, {}, None, [], {}, NOW)
    assert len(report["coins"]) == len(CFG["watchlist"])
    publish(report, candles, {}, NOW, data_dir=tmp_path, site_dir=tmp_path, persist=False)
    page = (tmp_path / "index.html").read_text()
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page
    assert 'href="javascript:' not in page


def test_trade_calls_are_scored_by_direction():
    long_call = "LONG $SOL/USDT | Cross 25X\nEntry: 140.5\nTargets: 145 - 150 - 158\nStop-loss: 134"
    short_call = "#XRP/USDT #SHORT\nEntry zone: 0.56 - 0.57\nTargets: 0.54, 0.52\nLeverage 10x\nSL: 0.59"
    assert sentiment.score_text(long_call) == sentiment.SIGNAL_STRENGTH
    assert sentiment.score_text(short_call) == -sentiment.SIGNAL_STRENGTH
    pats = sentiment.coin_patterns(CFG["watchlist"])
    assert sentiment.mentioned_coins(long_call, pats) == ["SOL"]
    assert sentiment.mentioned_coins(short_call, pats) == ["XRP"]
    assert sentiment.mentioned_coins("$LINKUSDT breakout, #DOT next", pats) == ["LINK", "DOT"]


def test_adverts_and_recaps_are_ignored():
    assert sentiment.is_noise("Free $PUMP signal: Target 1 hit, +78% profit! Join our VIP now")
    assert sentiment.is_noise("Get Free Lifetime VIP Access when you deposit $250 with our broker")
    assert sentiment.is_noise("#ETHUSDT 330% profit (10x)")
    # A real call forwarded from a VIP channel still counts.
    assert not sentiment.is_noise("Forwarded from VIP MEMBERSHIP\n#BTC/USDT #LONG Entry 62000 Targets 64000 SL 60500")
    assert not sentiment.is_noise("BTC holding the 4h trendline, bull case is a reclaim of 64k")


def test_channel_weight_changes_influence():
    strong = social.Post("telegram", "good", "$SOL looks bullish", NOW, "", 0, source_weight=1.5)
    weak = social.Post("telegram", "bad", "$SOL looks bearish", NOW, "", 0, source_weight=0.3)
    per_coin, _ = sentiment.aggregate([strong, weak], CFG["watchlist"], CFG["sources"]["weights"], now=NOW)
    assert per_coin["SOL"]["score"] > 0.3


def test_unlisted_coin_is_skipped_not_fatal():
    from bot.market import Market

    class FakeExchange:
        markets = {"BTC/USDT": {}, "ETH/USDT": {}}

        def load_markets(self):
            return self.markets

        def fetch_tickers(self, symbols):
            assert "TON/USDT" not in symbols
            return {s: {"last": 1.0, "percentage": 0.5, "quoteVolume": 1e9} for s in symbols}

    m = Market.__new__(Market)
    m.spot, m.quote = FakeExchange(), "USDT"
    assert set(m.tickers(["BTC", "ETH", "TON"])) == {"BTC", "ETH"}


def test_ai_labels_override_word_scoring():
    posts = [
        social.Post("telegram", "a", "ETH/USDT SHORT Entry Zone - Join Fast", NOW, ""),
        social.Post("telegram", "b", "CRYPTO FEAR AND GREED INDEX 59 Neutral, BTC ranging", NOW, ""),
        social.Post("news", "c", "Solana rallies as ETF inflows grow", NOW, ""),
    ]
    labels = {
        0: {"i": 0, "coins": ["ETH"], "stance": "bearish", "confidence": 0.9, "advert": True},
        1: {"i": 1, "coins": ["BTC"], "stance": "neutral", "confidence": 0.2, "advert": False},
        # post 2 has no label: falls back to word scoring
    }
    per_coin, mood = sentiment.aggregate(posts, CFG["watchlist"], CFG["sources"]["weights"],
                                         now=NOW, ai_labels=labels)
    assert per_coin["ETH"]["mentions"] == 0          # advert skipped
    assert per_coin["BTC"]["score"] == 0.0           # neutral, not "fear" = bearish
    assert per_coin["SOL"]["score"] > 0.3            # word scoring fallback
    assert mood["mentions"] == 2


def test_ai_label_posts_without_key_or_on_error(monkeypatch):
    from bot import ai

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    labels, status = ai.label_posts([], "m")
    assert labels is None and status.startswith("off")

    monkeypatch.setenv("GEMINI_API_KEY", "k")

    def boom(*a, **k):
        raise ai.requests.ConnectionError("down")
    monkeypatch.setattr(ai.requests, "post", boom)
    posts = [social.Post("news", "x", "BTC up", NOW, "")]
    labels, status = ai.label_posts(posts, "m")
    assert labels is None and status.startswith("failed")


def test_ai_label_posts_parses_response(monkeypatch):
    import json as _json
    from bot import ai

    monkeypatch.setenv("GEMINI_API_KEY", "k")
    seen = {}

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            out = [{"i": 0, "coins": ["btc"], "stance": "bullish", "confidence": 1, "advert": False}]
            return {"candidates": [{"content": {"parts": [{"text": _json.dumps(out)}]}}]}

    def fake_post(url, headers, json, timeout):
        seen["key_in_header"] = headers.get("x-goog-api-key") == "k" and "k" not in url
        return Resp()
    monkeypatch.setattr(ai.requests, "post", fake_post)
    labels, status = ai.label_posts([social.Post("news", "x", "BTC up", NOW, "")], "m")
    assert seen["key_in_header"] and status.startswith("ok")
    assert ai.stance_score(labels[0]) == 1.0


def test_openai_compatible_provider(monkeypatch):
    from bot import ai

    monkeypatch.setenv("AI_API_KEY", "k")
    seen = {}

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            content = '```json\n{"posts": [{"i": 0, "coins": ["ETH"], "stance": "bearish", "confidence": 0.5, "advert": false}]}\n```'
            return {"choices": [{"message": {"content": content}}]}

    def fake_post(url, headers, json, timeout):
        seen["url"], seen["auth"], seen["model"] = url, headers["Authorization"], json["model"]
        return Resp()
    monkeypatch.setattr(ai.requests, "post", fake_post)
    labels, status = ai.label_posts([social.Post("news", "x", "ETH weak", NOW, "")], "glm-4.7-flash",
                                    "openai_compatible", "https://api.z.ai/api/paas/v4/")
    assert seen == {"url": "https://api.z.ai/api/paas/v4/chat/completions", "auth": "Bearer k",
                    "model": "glm-4.7-flash"}
    assert status.startswith("ok") and labels[0]["stance"] == "bearish"


def test_local_model_labels():
    from bot import local_model

    posts = [
        social.Post("telegram", "a", "Free signal: TP1 hit +80% profit! Join VIP", NOW, ""),
        social.Post("telegram", "b", "#SOL/USDT #SHORT Entry 140 Targets 130 SL 146", NOW, ""),
        social.Post("news", "c", "Bitcoin looks ready to fly, accumulate", NOW, ""),
    ]

    def fake(texts, batch_size):
        assert len(texts) == 3
        return [{"label": "Bullish", "score": 0.9}, {"label": "Bullish", "score": 0.6},
                {"label": "Bullish", "score": 0.8}]

    labels, status = local_model.label_posts(posts, CFG["watchlist"], classify=fake)
    assert status.startswith("ok")
    assert labels[0]["advert"] is True
    assert labels[1]["stance"] == "bearish" and labels[1]["coins"] == ["SOL"]  # explicit SHORT wins
    assert labels[2]["stance"] == "bullish" and labels[2]["coins"] == ["BTC"]

    def broken(texts, batch_size):
        raise OSError("no model")
    labels, status = local_model.label_posts(posts, CFG["watchlist"], classify=broken)
    assert labels is None and status.startswith("failed")


def test_teasers_brags_and_empty_posts_are_noise():
    assert sentiment.is_noise("ETH/USDT SHORT 🚫 Entry Zone - Target 👇 ⚡️ https://t.me/+UoPySxz3ic42ZDNk Join Fast | Targets ☝️")
    assert sentiment.is_noise("#WINUSDT 130% 10X")
    assert sentiment.is_noise("#KASUSDT 1H")
    assert not sentiment.is_noise("🔴 SHORT $ZEC/USDT | Cross 30X ✅ Entry: 1340 🎯 TP: 1310 - 1280 - 1240 🛑 SL: 1410")


# ---------- emerging-token scanner ----------

def _series(closes, start_h=-800):
    rows, prev = [], closes[0]
    for i, c in enumerate(closes):
        rows.append([(NOW + timedelta(hours=start_h + 4 * i)).timestamp() * 1000,
                     prev, max(prev, c) * 1.01, min(prev, c) * 0.99, c, 100.0])
        prev = c
    return rows


def test_gem_prefilter_skips_stables_big_pumped_and_unlisted():
    from bot import emerging
    e = CFG["emerging"]
    row = lambda sym, name="Token", mcap=5e7, vol=5e6, ch7=10: {  # noqa: E731
        "symbol": sym.lower(), "name": name, "market_cap": mcap, "total_volume": vol,
        "price_change_percentage_7d_in_currency": ch7, "ath_change_percentage": -50}
    rows = [row("GEM"), row("USDX", "Some USD"), row("WBTC", "Wrapped Bitcoin"), row("BIG", mcap=5e9),
            row("PUMP", ch7=400), row("OFF"), row("LINK")]
    picks = emerging.prefilter(rows, {"GEM", "USDX", "WBTC", "BIG", "PUMP", "LINK"}, set(CFG["watchlist"]), e)
    assert [p["symbol"] for p in picks] == ["GEM"]
    assert emerging.narrative_of(["Ethereum Ecosystem", "Binance Alpha Spotlight", "DePIN"]) == "DePIN"


def test_gem_contract_check(monkeypatch):
    from bot import emerging
    assert emerging.contract_check({})[0] is True                       # native coin
    assert emerging.contract_check({"sui": "abc"})[0] is None            # not covered

    class R:
        def __init__(self, info): self.info = info
        def raise_for_status(self): pass
        def json(self): return {"result": {"0xabc": self.info}}
    monkeypatch.setattr(emerging.time, "sleep", lambda s: None)
    monkeypatch.setattr(emerging.requests, "get", lambda *a, **k: R({"is_open_source": "1", "sell_tax": "0"}))
    assert emerging.contract_check({"ethereum": "0xABC"})[0] is True
    monkeypatch.setattr(emerging.requests, "get", lambda *a, **k: R({"is_honeypot": "1", "sell_tax": "0.2"}))
    ok, note, _ = emerging.contract_check({"ethereum": "0xABC"})
    assert ok is False and "honeypot" in note and "tax" in note


def test_emerging_risk_rating():
    from bot import emerging
    safe = emerging.risk_profile({"market_cap": 2e9}, 2.0, 5e7)
    wild = emerging.risk_profile({"market_cap": 3e7, "circulating_supply": 2, "total_supply": 10,
                                  "ath_change_percentage": -95}, 12.0, 5e5)
    assert safe[1] == "Low" and wild[1] == "Very high"
    assert any("unlocked" in n for n in wild[2])


def test_emerging_timing_states():
    from bot import emerging
    up = [1.0 * 1.004 ** i for i in range(200)]
    up = [c * (1 + 0.01 * ((i % 6) - 3) / 3) for i, c in enumerate(up)]   # wiggle so RSI isn't 100
    c4h, c1d = _series(up), _series([1.0 * 1.01 ** i for i in range(120)])
    trend, _ = analysis.trend_score(c4h, c1d)
    plan = emerging.timing_plan(c4h, c1d, trend, "High", CFG["risk"], 10)
    assert plan["status"] in ("Enter zone", "Wait for pullback")
    assert plan["stop"] < plan["entry"] < plan["tp1"] < plan["tp2"]
    assert plan["max_loss"] <= CFG["risk"]["account_size"] * 0.005 + 1e-6   # High risk -> 0.5%
    assert plan["notional"] <= CFG["risk"]["account_size"] * 0.10 + 1e-6

    down = list(reversed(up))
    c4h, c1d = _series(down), _series([1.0 * 0.99 ** i for i in range(120)])
    trend, _ = analysis.trend_score(c4h, c1d)
    assert emerging.timing_plan(c4h, c1d, trend, "High", CFG["risk"], 10)["status"] == "Avoid"

    hot = [1.0] * 150 + [1.0 * 1.03 ** i for i in range(1, 51)]
    c4h = _series(hot)
    trend, _ = analysis.trend_score(c4h, c1d)
    assert emerging.timing_plan(c4h, c1d, trend, "High", CFG["risk"], 10)["status"] == "Take profit"


def test_gem_rating_safety_predictions_and_alerts():
    from bot import emerging
    from bot.demo import FakeMarket, fake_emerging
    scan = fake_emerging()
    scan["bases"][1]["cg"]["fully_diluted_valuation"] = scan["bases"][1]["cg"]["market_cap"] * 10
    tokens = emerging.rerate(scan, FakeMarket(NOW), CFG, ["$FET looks strong"], [])
    by = {t["symbol"]: t for t in tokens}
    assert by["PLUME"]["safe"] is False and "Most supply unlocked" in by["PLUME"]["failed_checks"]
    assert "Fair valuation" in by["VIRTUAL"]["failed_checks"]
    assert tokens[0]["safe"] and not tokens[-1]["safe"]
    assert any("talk about it" in n for n in by["FET"]["potential_notes"])
    for t in tokens:
        if t["scenarios"]:
            cases = {s["case"]: s for s in t["scenarios"]}
            assert cases["Bear"]["pct"] < 0 < cases["Base"]["pct"] < cases["Stretch"]["pct"]
    msgs = emerging.morning_messages(tokens, scan["hot_narratives"], 5, None)
    text = "\n".join(msgs)
    assert "Morning gems" in msgs[0] and "PLUME" in msgs[-1] and all(len(m) < 4096 for m in msgs)
    state = {}
    first = emerging.status_alerts(tokens, state)
    assert all("PLUME" not in m for m in first)                         # unsafe tokens never alert
    assert emerging.status_alerts(tokens, state) == []


# ---------- track record and backtest ----------

def _bars(path, start_h=0):
    """Candles from (high, low, close) tuples."""
    rows, prev = [], path[0][2]
    for i, (h, l, c) in enumerate(path):
        rows.append([(NOW + timedelta(hours=start_h + 4 * i)).timestamp() * 1000, prev, h, l, c, 1.0])
        prev = c
    return rows


def test_trade_rules_stop_targets_and_breakeven():
    from bot.tracker import manage_trade
    flat = [(100.5, 99.5, 100)] * 3
    # Stop first when one candle touches both.
    r = manage_trade(_bars(flat + [(125, 89, 100)]), 2, 100, 90, 120, 140, exit_on_ema20=False)
    assert r["status"] == "stopped" and r["result_r"] < -1
    # Target 1 (half at +2R), then the stop moves to entry.
    r = manage_trade(_bars(flat + [(121, 99, 115), (116, 99.9, 101)]), 2, 100, 90, 120, 140, exit_on_ema20=False)
    assert r["status"] == "target 1 then breakeven" and 0.9 < r["result_r"] < 1.0
    # Both targets: half at +2R and half at +4R = +3R before fees.
    r = manage_trade(_bars(flat + [(121, 99, 115), (141, 114, 139)]), 2, 100, 90, 120, 140, exit_on_ema20=False)
    assert r["status"] == "target 2" and 2.9 < r["result_r"] < 3.0
    # A limit entry that price never reaches doesn't count.
    r = manage_trade(_bars(flat + [(105, 101, 104)] * 31), 2, 100, 90, 120, 140, limit=True, exit_on_ema20=False)
    assert r["status"] == "never filled" and r["result_r"] == 0.0
    # Time exit after max_bars.
    r = manage_trade(_bars(flat + [(101, 99, 100.5)] * 61), 2, 100, 90, 120, 140, exit_on_ema20=False)
    assert r["status"] == "time exit"


def test_gem_picks_logged_once_and_settled():
    from bot import emerging
    from bot.demo import FakeMarket, fake_emerging
    tokens = emerging.rerate(fake_emerging(), FakeMarket(NOW), CFG, [], [])
    hist = []
    n = emerging.log_picks(hist, tokens, NOW - timedelta(days=3), emerging.DEFAULT_TIMING)
    assert n >= 1 and emerging.log_picks(hist, tokens, NOW, emerging.DEFAULT_TIMING) == 0
    h = hist[0]
    entry, stop = h["entry"], h["stop"]
    start = (NOW - timedelta(days=3, hours=4))
    c4h = _bars([(entry * 1.001, entry * 0.999, entry)] * 3 + [(entry * 1.01, stop * 0.99, stop)], 0)
    for i, c in enumerate(c4h):
        c[0] = (start + timedelta(hours=4 * i)).timestamp() * 1000
    emerging.settle_picks(hist, {h["symbol"]: c4h})
    if h["kind"] == "market":
        assert h["status"] == "stopped" and h["result_r"] < 0
    rec = emerging.picks_record(hist)
    assert rec["total"] == len(hist)


def test_backtest_signal_matches_live_rules():
    from bot import backtest, emerging
    from bot.demo import _walk
    c4h = _walk("bt", 10.0, 0.004, 0.012, 400, timedelta(hours=4), NOW)
    ind = backtest.indicators(c4h)
    agree = 0
    for i in range(300, 399, 7):
        sub = c4h[:i + 1]
        days = [r for j, r in enumerate(sub) if j % 6 == 5]
        trend, _ = analysis.trend_score(sub[-200:], days)
        live = emerging.timing_plan(sub[-200:], days, trend, "Medium", CFG["risk"], 10)
        sig = backtest.signal_at(c4h, ind, i, emerging.DEFAULT_TIMING)
        live_buy = live["status"] in ("Enter zone", "Wait for pullback")
        agree += (sig is not None) == live_buy
        if sig and live["status"] == "Enter zone":
            assert not sig["limit"] and abs(sig["entry"] - live["entry"]) < 1e-9
    assert agree >= 12  # of 15; small differences come from indicator warm-up and the daily candle


def test_backtest_tuning_needs_recent_confirmation():
    from bot import backtest
    base = {"trades": 50, "avg_r": 0.10}
    better = {"params": {**backtest.DEFAULT_TIMING, "stop_atr": 3.5}, "train": {"trades": 80, "avg_r": 0.5}}
    ev = {"default": {"test": base}, "variants": [{**better, "test": {"trades": 30, "avg_r": 0.3}}]}
    assert backtest.choose(ev)["adopted"] is True
    ev["variants"][0]["test"] = {"trades": 30, "avg_r": 0.05}       # worse on unseen months
    assert backtest.choose(ev)["adopted"] is False
    ev["variants"][0]["test"] = {"trades": 5, "avg_r": 0.9}         # too few to trust
    assert backtest.choose(ev)["adopted"] is False


def test_backtest_runs_end_to_end_on_made_up_prices():
    from bot import backtest
    from bot.demo import _walk
    data = {}
    for k in range(6):
        c4h = _walk(f"c{k}", 5.0, 0.002 * (k - 2), 0.02, 900, timedelta(hours=4), NOW)
        data[f"C{k}"] = (c4h, backtest.indicators(c4h), 1e6 * k)
    split = (NOW - timedelta(days=60)).timestamp() * 1000
    ev = backtest.evaluate(data, split, {"C0", "C1"})
    assert len(ev["variants"]) == 144 and ev["default"]["train"]["trades"] > 0
    report = {"coins": 6, "days": 150, "test_days": 60, "default": ev["default"], "choice": backtest.choose(ev)}
    assert "Weekly accuracy check" in backtest.summary_text(report)


def test_spike_is_not_chased_and_targets_respect_the_old_high():
    from bot import emerging
    quiet = [(0.335, 0.325, 0.33)] * 60 + [(0.37, 0.34, 0.36)] * 24          # base 0.34–0.37
    spike = [(0.40, 0.36, 0.39), (0.46, 0.39, 0.45), (0.54, 0.44, 0.50),
             (0.52, 0.45, 0.47), (0.49, 0.44, 0.45), (0.46, 0.44, 0.445)]
    c4h = _bars(quiet + spike)
    for c in c4h[-6:]:
        c[5] = 30.0                                                            # volume spike
    days = [r for j, r in enumerate(c4h) if j % 6 == 5]
    trend, _ = analysis.trend_score(c4h, days)
    plan = emerging.timing_plan(c4h, days, trend, "High", CFG["risk"], 10, ath=0.69)
    assert plan["status"] == "Wait for pullback" and plan.get("spike")
    assert abs(plan["entry"] - 0.37) < 1e-9 and plan["stop"] < 0.34
    assert abs(plan["tp1"] - 0.54) < 1e-9 and plan["tp2"] == 0.69
    assert "Don't buy the spike" in plan["action"]
    cg = {"price_change_percentage_7d_in_currency": 47, "price_change_percentage_30d_in_currency": 120,
          "market_cap": 2.4e8, "ath": 0.69, "atl": 0.094, "current_price": 0.445}
    base = {"watchers": 7476, "narrative": "DEX", "narrative_change": 1.0}
    calm, _ = emerging.potential_score(cg, base, trend, 1.2, 0, False, CFG["emerging"])
    spiky, notes = emerging.potential_score(cg, base, trend, 8.0, 0, False, CFG["emerging"],
                                            emerging.spike_info(c4h))
    assert spiky < calm and any("spike" in n for n in notes)
    assert not any("under the radar" in n.lower() or "possibly still early" in n.lower() for n in notes)


def test_solana_token_scan(monkeypatch):
    from bot import emerging

    class R:
        def __init__(self, info): self.info = info
        def raise_for_status(self): pass
        def json(self): return {"result": {"MINT": self.info}}
    monkeypatch.setattr(emerging.time, "sleep", lambda s: None)
    monkeypatch.setattr(emerging.requests, "get", lambda *a, **k: R({"freezable": {"status": "0"},
                                                                     "mintable": {"status": "0"}}))
    assert emerging.contract_check({"solana": "MINT"})[0] is True
    monkeypatch.setattr(emerging.requests, "get", lambda *a, **k: R({"freezable": {"status": "1"}}))
    ok, note, _ = emerging.contract_check({"solana": "MINT"})
    assert ok is False and "freeze" in note


def test_extended_month_wide_stop_chart_targets_and_meme_holder_checks():
    from bot import emerging
    # A coin that more than doubled in a month: never "buy now".
    px = [0.03 * 1.0045 ** k * (1 + 0.03 * ((k % 4) - 1.5) / 1.5) for k in range(200)]
    path = [(0.031, 0.029, 0.03)] * 20 + [(c * 1.01, c * 0.99, c) for c in px]
    c4h = _bars(path)
    days = [r for j, r in enumerate(c4h) if j % 6 == 5]
    days = [[d[0], d[1], d[2] * 1.03, d[3] * 0.97, d[4], 1] for d in days]   # ~6% daily range
    trend, _ = analysis.trend_score(c4h, days)
    plan = emerging.timing_plan(c4h, days, trend, "High", CFG["risk"], 10, ath=0.216)
    assert plan["status"] == "Wait for pullback" and "stretched to buy now" in plan["action"]
    assert plan["entry"] - plan["stop"] >= 0.8 * analysis.atr(days) - 1e-12   # outside a normal day
    assert plan["tp1"] <= max(c[2] for c in c4h[-42:]) + 1e-12 or plan["tp1"] > plan["entry"]
    sc = emerging.scenarios({"market_cap": 7.1e7, "ath": 0.216}, plan)
    assert all(s["case"] != "Old high" for s in sc)            # a 2025 high far away is not a target

    info = {"holders": [{"address": "0xa", "percent": "0.40"}, {"address": "0xb", "percent": "0.30"},
                        {"address": "0xdead", "percent": "0.9", "tag": "Binance"},
                        {"address": "0xc", "percent": "0.5", "is_locked": 1}]}
    assert abs(emerging.top_holder_share(info) - 0.70) < 1e-9
    base = {"narrative": "Meme", "top10_share": 0.7, "contract_ok": True, "age_days": 300}
    checks = {c["name"]: c["ok"] for c in emerging.safety_checks(
        {"market_cap": 7e7, "total_volume": 2e7, "circulating_supply": 1e9, "total_supply": 1e9,
         "fully_diluted_valuation": 7e7}, base, 5e6, 4.0, CFG["emerging"])}
    assert checks["Real product"] is False and checks["Spread across many holders"] is False


# ---------- trend signals ----------

def _trend_hist(closes):
    from datetime import date, timedelta
    d0 = date(2024, 1, 1)
    return [[(d0 + timedelta(days=i)).isoformat(), c, c] for i, c in enumerate(closes)]


def test_trend_long_in_uptrend_and_out_in_downtrend():
    import math
    from bot import trend
    up = [100 * math.exp(0.004 * i + 0.02 * math.sin(i)) for i in range(400)]
    down = up + [up[-1] * math.exp(-0.01 * i) for i in range(1, 200)]
    w_up = trend.weights({"BTC": _trend_hist(up), "ETH": _trend_hist(up)})
    w_dn = trend.weights({"BTC": _trend_hist(down), "ETH": _trend_hist(down)})
    assert w_up["BTC"]["target"] > 0.2 and w_up["BTC"]["full_exit"] < up[-1]
    assert w_dn["BTC"]["target"] == 0 and w_dn["BTC"]["next_add"] > down[-1]
    total = sum(d["target"] for d in w_up.values())
    assert total <= 1.0


def test_trend_signals_only_on_real_change():
    from bot import trend
    d = {"target": 0.30, "votes_on": 6, "votes": 8, "close": 100.0, "next_trim": 95.0,
         "full_exit": 80.0, "next_add": None}
    state = {}
    assert trend.signals({"BTC": d}, state, 1000) == []          # first run is silent
    assert trend.signals({"BTC": dict(d, target=0.32)}, state, 1000) == []   # small move
    msgs = trend.signals({"BTC": dict(d, target=0.0, full_exit=None, next_trim=None)}, state, 1000)
    assert len(msgs) == 1 and "SELL all BTC" in msgs[0]
    msgs = trend.signals({"BTC": dict(d, target=0.25)}, state, 1000)
    assert "BUY BTC" in msgs[0] and "250 USDT" in msgs[0]


def test_trend_paper_trading_charges_fees_and_follows_targets():
    from bot import trend
    closes = [100.0] * 50 + [100.0 + i for i in range(1, 100)]
    hist = {"BTC": _trend_hist(closes), "ETH": _trend_hist(closes)}
    w = trend.weights(hist)
    sim = trend.simulate(hist, w)
    assert sim["trades"] >= 1 and sim["equity"][-1] > 1.0
    flat = {"BTC": _trend_hist([100.0] * 120), "ETH": _trend_hist([100.0] * 120)}
    s2 = trend.simulate(flat, trend.weights(flat))
    assert s2["equity"][-1] <= 1.0


def test_trend_guard_pauses_buys_but_not_sells():
    from datetime import datetime, timezone
    from bot import trend
    now = datetime(2026, 10, 9, 2, tzinfo=timezone.utc)
    assert trend.guard({"drawdown": -5.0}, "2026-10-08", now)["buys_allowed"]
    g = trend.guard({"drawdown": -40.0}, "2026-10-08", now)
    assert not g["buys_allowed"] and "safety limit" in g["reasons"][0]
    assert not trend.guard({"drawdown": 0.0}, "2026-10-01", now)["buys_allowed"]          # stale prices
    assert not trend.guard({}, "2026-10-08", now, {"status": "weak", "sharpe_2y": -0.3})["buys_allowed"]
    d = {"target": 0.30, "votes_on": 6, "votes": 8, "close": 100.0, "next_trim": 95.0, "full_exit": 80.0, "next_add": None}
    state = {"announced": {"BTC": 0.10}}
    assert trend.signals({"BTC": d}, state, 1000, buys_allowed=False) == []
    assert trend.signals({"BTC": dict(d, target=0.0)}, state, 1000, buys_allowed=False)  # sells still go


def test_alt_sleeve_gate_funding_and_daily_summary():
    import math
    from bot import altsleeve
    from datetime import date, timedelta
    d0 = date(2025, 1, 1)
    days = [(d0 + timedelta(days=i)).isoformat() for i in range(400)]
    up = [10 * math.exp(0.003 * i + 0.03 * math.sin(i)) for i in range(400)]
    cache = {"coins": {f"C{k}": [[d, p * (1 + k / 10), 5e6] for d, p in zip(days, up)] for k in range(3)},
             "first": {f"C{k}": "2024-01-01" for k in range(3)}}
    btc_up, btc_down = up, list(reversed(up))
    w = altsleeve.weights(cache, btc_up, days[-1])
    assert w["gate"] and 0 < w["total"] <= altsleeve.SLEEVE
    assert all(d["target"] <= altsleeve.CAP * altsleeve.SLEEVE + 1e-9 for d in w["coins"].values())
    assert altsleeve.weights(cache, btc_down, days[-1])["total"] == 0          # BTC downtrend: cash
    hot = altsleeve.weights(cache, btc_up, days[-1], {c: 0.002 for c in cache["coins"]})
    assert abs(hot["total"] - w["total"] / 2) < 1e-9                          # crowded longs: halved
    state = {}
    assert "now live" in altsleeve.changes(w, state, 1000)
    assert altsleeve.changes(w, state, 1000) is None                         # nothing new
    msg = altsleeve.changes(altsleeve.weights(cache, btc_down, days[-1]), state, 1000)
    assert "SELL all" in msg and "200-day" in msg


def test_stale_altcoin_prices_never_sell(tmp_path, monkeypatch):
    import json
    from datetime import datetime, timezone
    import bot.main as m
    monkeypatch.setattr(m, "DATA_DIR", tmp_path)
    cache = {"coins": {f"C{k}": [["2026-10-07", 1.0, 5e6]] for k in range(30)}, "first": {}}
    (tmp_path / "alt_daily.json").write_text(json.dumps(cache))
    state = {"alt_started": True, "alt_announced": {"C1": 0.02, "C2": 0.015}}
    (tmp_path / "trend_state.json").write_text(json.dumps(state))

    class Spot:
        def load_markets(self): pass
        def fetch_tickers(self): return {}
    class Mk:
        quote, spot = "USDT", Spot()
    rep = {"source_health": {}, "trend": {}}
    msgs = m.run_alt_sleeve(Mk(), rep, datetime(2026, 10, 9, 6, tzinfo=timezone.utc), [1.0] * 300,
                            "2026-10-08", 1000.0, {"buys_allowed": True}, tmp_path / "trend_state.json")
    assert msgs == [] and "nothing sold" in rep["source_health"]["altcoin portion"]
    assert json.loads((tmp_path / "trend_state.json").read_text())["alt_announced"] == state["alt_announced"]


def test_scorecard_scores_sells_and_buys_against_doing_nothing():
    from datetime import datetime, timezone
    from bot import scorecard
    log = []
    scorecard.record(log, {"SOL": 0.02, "AVAX": 0.0}, {"SOL": 0.0, "AVAX": 0.03}, {"SOL": 100.0, "AVAX": 10.0},
                     "2026-10-08", datetime(2026, 10, 9, tzinfo=timezone.utc), "alts")
    closes = {"SOL": {"2026-10-09": 95.0, "2026-10-15": 90.0}, "AVAX": {"2026-10-09": 10.5, "2026-10-15": 9.0}}
    scorecard.score(log, closes)
    sol = next(r for r in log if r["coin"] == "SOL")
    avax = next(r for r in log if r["coin"] == "AVAX")
    assert sol["action"] == "sell" and sol["edge_7d"] == 9.9        # avoided a 10% drop, minus 0.1% fee
    assert avax["action"] == "buy" and avax["edge_7d"] == -10.1
    s = scorecard.summary(log)
    assert s["scored"] == 2 and s["verdict"] == "not enough signals yet"


def test_p2p_routes_skip_small_or_new_traders_and_charge_transfer():
    from bot import p2p
    assert p2p.usable(p2p._ad("Bybit", "ask", 1350, 1000, 1e6, 500, 0.99, "x", "bank"))
    assert not p2p.usable(p2p._ad("Bybit", "ask", 1350, 1000, 5000, 500, 0.99, "x", "bank"))     # too small
    assert not p2p.usable(p2p._ad("Bybit", "ask", 1350, 1000, 1e6, 12, 1.0, "x", "bank"))        # too new
    ask = {"price": 1350.0}
    best = {"Bybit": {"ask": ask, "bid": {"price": 1340.0}}, "OKX": {"ask": None, "bid": {"price": 1377.0}}}
    r = p2p.routes(best)
    top = r[0]
    assert (top["buy"], top["sell"]) == ("Bybit", "OKX")
    assert 0.9 < top["gap"] < 2.0                    # 2% gap minus about 1% transfer cost on a small ticket
    assert r[-1]["gap"] < 0                          # same exchange: best bid below best ask


def test_cars_fair_value_and_deals():
    from carbot import cars
    assert cars.make_model("Clean standard 2005 Toyota Rav4") == ("toyota", "rav4")
    assert cars.make_model("Mercedes-Benz GLK 350 2012")[0] == "mercedes-benz"
    assert cars.year_of("Clean standard 2005 Toyota Rav4") == 2005
    base = dict(source="t", make="toyota", model="camry", condition="Foreign Used", mileage=None,
                inspected=False, accident=False, city="", url="u")
    pool = [base | dict(id=i, title="Toyota Camry", year=2012, price=p)
            for i, p in enumerate([10e6, 10.5e6, 11e6, 9.8e6, 10.2e6, 10.4e6])]
    cheap = base | dict(id=99, title="Toyota Camry", year=2013, price=8e6)
    scam = base | dict(id=98, title="Toyota Camry", year=2012, price=3e6)
    crash = base | dict(id=97, title="Toyota Camry", year=2012, price=8.5e6, accident=True)
    allc = pool + [cheap, scam, crash]
    cars.fair_values(allc)
    assert 20 < cheap["below"] < 25
    ds = cars.deals(allc)
    assert ds[0] is cheap and scam not in ds and crash not in ds


def test_cars_focus_enugu_corolla():
    from carbot import cars
    mk = lambda **k: dict(source="t", id=k.get("id", 0), title="Toyota Corolla", make="toyota", model="corolla",
                          condition="", mileage=None, inspected=False, accident=False, url="u",
                          fair=None, below=None, comps=0) | k
    a = mk(id=1, year=2008, price=6e6, city="Enugu")
    b = mk(id=2, year=2008, price=6.5e6, city="Lagos")
    c = mk(id=3, year=2012, price=8e6, city="Enugu")
    old = cars.FOCUS["area"]
    try:
        cars.FOCUS["area"] = "Enugu"
        assert cars.in_focus(a) and not cars.in_focus(b) and not cars.in_focus(c)
        assert "in Enugu: 1 listed" in cars.focus_text([a, b, c])
        cars.FOCUS["area"] = None
        assert cars.in_focus(b) and not cars.in_focus(c)
        t = cars.focus_text([a, b, c])
        assert "in Nigeria: 2 listed" in t and "2008: usual" in t
    finally:
        cars.FOCUS["area"] = old
