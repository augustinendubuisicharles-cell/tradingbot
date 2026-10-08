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
