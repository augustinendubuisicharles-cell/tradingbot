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

### 4. AI post reading (optional, free)
1. Go to aistudio.google.com, sign in with a Google account and click **Get API key → Create API key**.
2. In GitHub, add it as a repository secret named `GEMINI_API_KEY`.

With the key, Google's Gemini model reads each post every 4 hours. It works out which coins the post is about, whether the author is bullish or bearish and how strongly, and whether the post is just an advert. Without the key, the bot falls back to counting bullish and bearish words.

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

## Limits to know

- **No direct X feed.** X has no free API, and scraping it breaks X's terms. If you later pay for X API access, it can be added as another source.
- Reddit sometimes blocks requests from GitHub's servers. When that happens, the dashboard's "Data sources" table shows it and the other sources still work.
- GitHub can start scheduled runs a few minutes late.
- A private repo gets 2,000 free Actions minutes a month. This setup uses about 1,800. A public repo has no limit.
- Without the Gemini key, sentiment comes from a free word-based model (VADER plus crypto slang), which can't detect sarcasm. Gemini's free tier has daily limits; the bot only calls it once every 4 hours.

## Run locally

```
pip install -r requirements.txt
python -m bot.main demo          # offline preview with made-up data -> demo/index.html
python -m pytest -q              # tests
```

Ideas only, not financial advice.
