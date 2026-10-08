# TradingBot

A free crypto signals bot. Every 4 hours it reads trader Telegram channels, Reddit and crypto news. It scores the sentiment of what it reads and combines that with Bitget price trends and futures positioning. It then suggests **low-risk spot trades**, each with an entry, a stop-loss, targets and a position size. The results go to a web dashboard and to your phone through Telegram. Between the 4-hour updates, it checks every 30 minutes for strong setups, sharp price moves and sentiment swings.

It **never places trades** and needs no exchange API key. It suggests spot trades only, because crypto futures are not available to UK retail traders.

Everything runs on free services: GitHub Actions (the scheduler), Cloudflare Pages (the dashboard) and a Telegram bot (alerts).

## One-time setup (about 15 minutes)

### 1. Telegram bot
1. In Telegram, open **@BotFather**, send `/newbot` and follow the prompts. It gives you a **bot token**. Keep it private.
2. Open your new bot and send it any message, for example "hi".

### 2. GitHub secrets
In the repository on github.com, go to **Settings → Secrets and variables → Actions**:
1. Under **Secrets**, add `TELEGRAM_BOT_TOKEN` with the token from BotFather.
2. Go to the **Actions** tab, choose **Find my Telegram chat id** and click **Run workflow**. Open the finished run, then open the "Print chat id" step and copy the number shown.
3. Back under **Secrets**, add `TELEGRAM_CHAT_ID` with that number.

### 3. Dashboard on Cloudflare Pages
1. Sign up free at dash.cloudflare.com, then go to **Workers & Pages → Create → Pages → Connect to Git** and pick this repository.
2. Build settings: framework **None**, build command **empty**, build output directory **`site`**.
3. Deploy. Cloudflare gives you a link like `https://tradingbot-xyz.pages.dev`.
4. Back in GitHub, under **Settings → Secrets and variables → Actions → Variables**, add `DASHBOARD_URL` with that link. The Telegram digest will then include it.
5. Optional: to keep the dashboard private, turn on Cloudflare Access for the site (free for up to 50 users).

### 4. AI post reading (free, nothing to set up)
Every 4 hours the bot runs CryptoBERT, a free open-source AI model trained on millions of crypto social posts, to judge whether each post is bullish, neutral or bearish. It runs inside the bot on GitHub, so there's no account, key or bill. Adverts and "target hit" recaps are filtered out, and explicit LONG/SHORT calls are read directly.

If you ever want a larger AI model, `config.yaml` can switch to Google Gemini or Zhipu GLM with an API key instead.

### 5. Start it
Go to the **Actions** tab, open **4-hour analysis** and click **Run workflow**. After that, it runs on its own.

## Changing what it watches

Edit `config.yaml` on GitHub:
- `watchlist`: the coins to analyse.
- `sources.telegram_channels`: the trader channels the bot reads, each with a `weight` (how much it counts). Adverts and "target hit" recaps are skipped; trade calls count as a bullish or bearish vote on that coin.
- `risk`: account size, risk per trade (default 1%), and the stop-loss distance.
- `alerts`: how strong a signal must be before you're pinged, and how often alerts can repeat.

## How a coin is scored (0–100)

| Part | Weight | What it measures |
|---|---|---|
| Trend | 35% | Price against its 4h and daily moving averages |
| Sentiment | 25% | Weighted mood of recent posts mentioning the coin (newer and more-viewed posts count more) |
| Momentum | 15% | RSI. Rewards healthy strength and penalises overbought or collapsing coins |
| Low volatility | 15% | Calmer coins score higher (lower risk) |
| Positioning | 10% | Futures funding rate. Crowded longs lower the score |

A coin only becomes a trade idea when it scores 65 or more, its trend is up, it isn't overbought, and it trades at least 20M USDT a day. Stops sit 2× the average 4h range below entry, and size is set so a stop-out loses about 1% of your account.

The dashboard keeps a **track record** of every idea (target hit, stopped, or expired after 14 days), so you can judge the signals before trusting them with real money.

## Morning hidden gems (every narrative)

Every morning at 07:00 UK time (06:00 in winter) the bot scans the 1,000 most-traded coins on CoinGecko, across every narrative (AI, DePIN, gaming, RWA, memes, layer 2s and the rest). It keeps small ones ($10M–$300M market cap) that Bitget lists, then checks the best 20 in depth and sends the top 5 to Telegram.

**Safety checks.** A token must pass all of these to be recommended:
- Real trading volume, on Bitget and overall
- At least 35% of supply already unlocked
- Fully diluted value no more than 4× market cap
- Contract scan (GoPlus): no honeypot, hidden owner, balance tricks or high taxes, and code that is public
- Not mid-pump (under +150% in a week, +60% in a day)
- At least 2 weeks of trading history on Bitget
- Volatility under control
- A real project website

**Growth potential (0–100)** comes from trend, recent gains, volume surges, how hot its narrative is, how early it still is (few CoinGecko watchers, not trending, rarely mentioned), room to grow (smaller cap) and how new it is.

**Prediction.** Each pick gets scenarios from the entry price: bear (stop-loss), base (target 1), stretch (target 2) and, where reachable, bull (back to its old high). Each shows the market cap it implies. These are scenarios, not promises.

**When to get in and out.** Timing is one of *Enter zone*, *Wait for pullback*, *Watch for breakout*, *Take profit*, *Exit signal* or *Avoid*. The 4-hourly runs refresh the timing with live prices, and Telegram pings you when a safe pick moves into a buy or an exit. Riskier tokens get smaller sizes (a stop-out costs 1% of the account for Low risk, down to 0.25% for Very high). Settings are under `emerging` in `config.yaml`.

## How accurate is it?

- **Real results.** Every gem pick that reaches a buy is logged and played forward against real Bitget prices (stop, targets, breakeven, trend exit, 10-day limit, fees). The dashboard shows the win rate and average result.
- **Weekly backtest.** Every Sunday the bot replays its buy and sell rules on the past year of 4-hour prices for about 100 Bitget coins, tests 72 setting variations, and sends a report card to Telegram. A better variation is only adopted if it also wins on the most recent 90 days, which it wasn't tuned on. This guards against settings that only look good on old data.
- **Spikes.** A one-day spike (+25% in 24 hours, or a big volume burst) is never a "buy now". The plan becomes a limit buy at the top of the range it broke out of, a stop under that range, the spike high as target 1 and the old all-time high as target 2.

## Limits to know

- **No direct X feed.** X has no free API, and scraping it breaks X's terms. If you later pay for X API access, it can be added as another source.
- Reddit sometimes blocks requests from GitHub's servers. When that happens, the dashboard's "Data sources" table shows it and the other sources still work.
- GitHub can start scheduled runs a few minutes late.
- A private repo gets 2,000 free Actions minutes a month. This setup uses about 1,800. A public repo has no limit.
- The 30-minute alert checks use quicker word-based scoring (VADER plus crypto slang). The 4-hourly analysis uses CryptoBERT, which reads up to about 100 words of each post.

## Run locally

```
pip install -r requirements.txt
python -m bot.main demo          # offline preview with made-up data -> demo/index.html
python -m pytest -q              # tests
```

Ideas only, not financial advice.
