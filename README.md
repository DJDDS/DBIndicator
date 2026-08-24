# Scanner — Kite-connected web dashboard

This runs your RSI + MACD + CMF + Relative Volume confluence Scanner against live
Zerodha data for NSE F&O stocks and shows current signals, live charts,
and an AI-generated summary on a simple web page you can open any time.
It needs to run continuously on a server (not your own laptop that goes
to sleep) — see Part 2 for how to get one.

**Reality check before you start:** Zerodha requires a fresh login every
trading day for security reasons — there's no way to eliminate this step
entirely without storing your password, which this app deliberately does
not do. So the daily routine is: open the dashboard, click one login
button, sign in with your Zerodha password + 2FA (~30 seconds), and it
runs on its own for the rest of the day.

## What's in this version

- **Timeframes are pinned per surface**, not chosen from a dropdown — the
  watchlist runs on **daily**, the intraday panel on **15-min against the
  4-hour trend**. See "How the timeframes work" below.
- **F&O-only watchlist**: on the Settings page, "Load current F&O list
  from Kite" pulls the *exact, live* list of NSE stocks currently
  eligible for futures & options trading straight from Kite's own
  instrument list — not a hardcoded list that can go stale as NSE
  periodically revises F&O eligibility.
- **Everything tunable from the browser**: watchlist, MACD preset/custom
  values, RSI/EMA/Bollinger lengths, how many of the 4 parameters must
  agree, and scan frequency all live-update from the **Settings** page —
  no editing `.env` or restarting the server.
- **Real charts**: click any row (or "Chart →") to open a candlestick
  chart with the 9 EMA and Bollinger mid-band overlaid, plus separate
  RSI and MACD panes, all synced and zoomable (TradingView's open-source
  Lightweight Charts library).
- **AI Insights**: an optional panel that asks Claude to summarize the
  latest scan in plain English — which stocks just signaled and why,
  which are close to aligning, whether the session looks unusually quiet
  or busy. Uses your own Anthropic API key; the panel is simply hidden
  if you don't set one.
- **Alerts**: get notified the moment a Bullish/Bearish confluence
  signal fires, instead of having to watch the table — see the dedicated
  section below.

---

## Alerts

Two channels, and you can use either or both:

- **In-page (works automatically, no setup)** — while the dashboard tab
  is open, a toast banner and a short beep fire for every new signal.
  This is free but only works while you're actually looking at the tab.
- **Telegram (recommended — works even with the app closed)** — a
  message lands in Telegram on your phone the moment a signal fires.
  Setup takes about two minutes:
  1. On Telegram, message **@BotFather**, send `/newbot`, and follow the
     prompts (pick any name/username). It replies with a token like
     `123456789:AAF-abc...` — put that in `.env` as `TELEGRAM_BOT_TOKEN`,
     then restart the app.
  2. Send any message (e.g. "hi") to your new bot on Telegram — bots
     can't message you first, so this step is required.
  3. On the dashboard's **Settings** page, click **"Find my chat ID"** —
     it reads Telegram's own API to find the chat you just started and
     shows you the number. Put that in `.env` as `TELEGRAM_CHAT_ID`, then
     restart the app once more.
  4. Use the **"Send test alert"** button on Settings to confirm it
     works, any time.

Both channels are deduplicated per candle — you get exactly one alert
per fresh signal on its closing candle, not one every scan interval
while it stays the most recent signal.

## News (optional)

Attaches recent headlines to any **Confirmed** row and fires the same
Telegram/in-page alert as a fresh signal the moment a genuinely new
article shows up for one of those symbols. Off entirely unless you set
it up — no news source is built in by default.

Headlines get their own **News card** on the dashboard, listing every
recent headline across your currently-Confirmed symbols with its source
and time. It started as a per-row table column, but since headlines are
only fetched for Confirmed rows that column mostly rendered an em-dash —
one card in a fixed place reads far better than a mostly-empty column.

1. Sign up free at [marketaux.com](https://www.marketaux.com) (no card
   needed) and grab your API key from the dashboard.
2. Put it in `.env` as `MARKETAUX_API_TOKEN`, then restart the app.

That's it — no chat ID or bot setup needed here, it reuses whatever
Telegram config you already have from the Alerts section above (if any;
without Telegram configured, news still shows on the dashboard and logs
to the in-page toast, it just won't push to your phone).

**A real limit worth knowing:** the free Marketaux plan allows 100
requests/day and returns at most 3 articles per request, *total* —
shared across however many of your symbols are Confirmed at once, not
3 each. This app spends that budget deliberately: only Confirmed
symbols are queried (not the whole watchlist), throttled to roughly
every 15 minutes and capped well under the daily limit, so on a quiet
day you'll see news for everything Confirmed, and on a day with many
signals firing at once you'll see the handful Marketaux itself
considers most relevant. Upgrading to a paid Marketaux plan (more
requests/day and more articles/request) is just a higher tier on their
side — nothing in this app needs to change.

---

## VWAP & Anchored VWAP

Both are plotted on the **Chart** page (teal = session VWAP, dashed
magenta = anchored VWAP, measured since the current trend leg began).
They used to also appear as text under Close in the watchlist table; that
was removed when the table was de-cluttered, because **Ext** already
carries the actionable version of the same information — how far past
VWAP price has run, in ATR units, in the row's own direction.

## Journal-based confidence score

If you've logged paper trades in the Signal Journal (**/journal** page), a small
**📓** badge now appears next to Confirmed rows showing the REALIZED win
rate your own resolved trades have had on that exact setup (direction +
how many of the 4 parameters aligned) — e.g. "📓67%" means your own
Bullish/3-of-4 trades have won 67% of the time so far. It only appears
once at least 5 of your own resolved trades share that setup, so it's
never a misleading number from 1-2 trades. The **/journal** page itself
now also has two breakdown tables: win rate by setup, and win rate by
each optional agreement filter (sector/breadth/candle-pattern/higher-timeframe) — a real, walk-forward answer to "does turning
this filter on actually help", from your own trading, not a generic
claim.

## MACD histogram momentum

The MACD column now carries a small ▲/▼ badge showing whether the
histogram itself is **rising or falling** vs. the previous bar — is the
crossover's momentum accelerating or already fading. This is genuinely
different from the existing macd_line-vs-signal-line check (which just
says which side of zero the histogram is on — mathematically the same
thing as "is the histogram positive"), so it's a new, second read rather
than a restatement. Off by default; turn on "Require MACD histogram
momentum agreement" on the Settings page to have a row whose momentum is
fading against its own direction lose its Confirmed status.

## How the timeframes work (read this first)

Every surface is **pinned** to the timeframe that matches its job. There is
no global timeframe dropdown any more — that one knob was the biggest
source of confusion in this app, because the same badge meant different
things depending on a setting you'd changed days earlier.

| Surface | Timeframe | Why |
|---|---|---|
| Watchlist table, OI Screener, Best Entries, alerts, journal | **daily** | The bar a BTST/swing decision is actually made on, and the only one where Close@, NR7 and Delivery mean what their names say |
| Intraday panel | **15-minute, cross-checked against 4-hour** | Entry timing, with the 4-hour trend as the confluence check |
| Chart & Backtest pages | your choice | Research surfaces, where switching timeframe is the point |

60-minute was dropped: it sat between the two timeframes that actually do
a job, adding scan load and screen clutter without answering a question
the other two didn't already answer.

Daily also finally has a **weekly** higher-timeframe check. Before this it
had none at all, which meant the HTF gate silently did nothing on exactly
the timeframe the watchlist now runs on.

## The four parameters

**RSI · MACD · Chaikin Money Flow · Relative Volume.**

CMF replaced the old *EMA9 vs Bollinger mid* vote. That one was a plain
moving-average crossover wearing a Bollinger label — nothing in it read
the bands at all — and being a third transform of the same closing-price
series it added little that RSI and MACD didn't already say. Your own
Auto-Weight run scored it 0.0%, though on only 4 trades, so treat that as
suggestive rather than evidence; the structural argument is the real one
(see `NEXT_HORIZON_RESEARCH.md` Finding 1 on correlated votes).

The vote is now **2 price reads + 2 volume reads** instead of 3 price + 1
volume — genuinely more independent evidence behind the same count.
Bollinger itself didn't leave: the bands still drive the breakout state
and the band-width coiling read, which is what Bollinger Bands are
actually built to measure.

Defaults are now **4-of-4**, with the entry-location and ATR-floor gates
**on** — fewer candidates, each of which is early rather than chasing and
in a stock that actually moves. Loosen on the Settings page if that's too
tight for you.

## Backtest costs and holdout (research Finding 2)

Backtest returns are now **net of costs by default**. Every horizon's
return has a round-trip drag subtracted: `cost_pct` (0.08% default, from
Zerodha's published stock-futures charges) plus `slippage_pct` twice
(0.05% per side, because you cross the spread entering and exiting).

This matters more than it sounds. A trade showing +0.05% gross is
**−0.13% net** — the sign flips. Options are far worse than futures here,
because STT and exchange charges are levied on premium rather than
underlying notional; test an options strategy with a much higher figure.

Drawdown (`mae_pct`) is deliberately left **gross** — it describes raw
adverse price action, which is a property of the market rather than of
your cost structure.

`holdout_pct` adds the overfitting discipline: split the window, tune
freely against the earlier portion, then look at the holdout **once** and
accept what it says. A holdout you re-check after every tweak has quietly
become training data.

## BTST / STBT panel

Replaces the old "High Conviction" card, which stacked several conditions
that move together in practice (a 4-of-4 row is already likely to be above
VWAP and volume-heavy), so it looked far more selective than it was — and
its own docstring admitted none of it had been backtested. It also answered
a question nobody asked: *which row has the most things lit up?*

The replacement answers the question you're actually trading: **is this
worth holding overnight?** That's a different bar, because an overnight
position carries gap risk an intraday one doesn't.

**One hard requirement: a strong close in the row's own direction.**
Holding something overnight that closed weak into the bell is the opposite
of the setup, no matter how many indicators agree. Everything else is
supporting evidence — counted, shown, never silently decisive.

And every candidate **argues its own case**. Instead of one opaque flag you
get each check in plain words, marked met (✓), not met (✗), or unknown (·):

```
BTST  RELIANCE  1412.65                        7 of 9 checks
  ✓ Closed at 94% of the day's range - buyers held it into the bell
  ✓ Not extended - you're not carrying an already-stretched move overnight
  ✓ Moves enough to be worth the gap risk (ATR 2.4%)
  ✗ Against the weekly trend - a gap against you is more likely
  · No delivery data (NSE publishes after the close)
```

Missing data reads as *unknown*, never as a failure — same convention as
every gate in the app.

**Read it late in the session.** The daily bar is still forming until 15:30,
so "closed strong" is provisional before then; a name here at noon can fail
the test by the bell. The panel says so itself.

## Which gates actually earn their place?

On the Backtest page. Until now, none of the optional gates had ever been
measured — the app had far more machinery than evidence about any of it.

It runs a **baseline backtest with every gate off**, then **one run per gate
with only that gate on**, and reports what each did to your win rate, net of
costs, on your own watchlist. One click instead of hand-running the backtest
twice per gate.

Read it honestly, and the panel says all of this on screen:

- Each gate is measured **in isolation**, so it can't see two gates that
  only help together, or that overlap and double-count. A full interaction
  study is 2^N runs; this is N+1.
- **Fewer trades isn't automatically worse.** A gate that cuts 60% of trades
  for +3 points of win rate may or may not suit you, so both numbers sit
  side by side rather than collapsed into one score.
- A big delta on a handful of trades is noise, which is why the trade count
  is on every row.

## Best Entries panel

The screener answers "does this stock have a signal?" The **⚡ Best
Entries** card at the top of the dashboard answers the second question you
were otherwise left doing by eye: *given several rows that all currently
qualify, which are the better entries right now?*

It takes only rows that already earned a Confirmed ✓ — it can never
promote something the screener didn't surface — and re-orders them by a
0-100 score built from six reads already sitting on each row:

| Component | Max | What earns it |
|---|---|---|
| Entry location | 30 | Price still near/behind VWAP rather than ATRs past it |
| Big candle | 25 | A range-expansion bar in this row's direction, level cleared |
| Volatility | 15 | ATR comfortably above your floor — the stock actually moves |
| Coiling | 15 | Band width tight / NR7 — a squeeze that hasn't released yet |
| Strong close | 10 | Closed decisively in this row's own direction |
| Delivery | 5 | NSE delivery % above your mark |

Entry location carries the most weight deliberately: it's the one
component that separates catching a move from chasing one. A component
with no reading scores a neutral middle value rather than zero, so a
missing number never ranks a stock below one that actively looks bad.

Hover any row for the full per-component breakdown — the score is never a
black box.

**These weights are reasoned, not backtested.** They encode a specific
opinion (entry location matters most; a stock too quiet to move is a poor
entry however many indicators agree), but nobody has measured this exact
combination against historical outcomes. Treat it as a sensible way to
order your shortlist, not as a validated edge — the same caveat that
applies to "High Conviction."

## Anticipatory signals (catch a big move before it happens)

RSI/MACD/EMA-BB/CMF are all confirmatory — smoothed derivatives of price
that tell you a move is already under way. These four are genuinely
ANTICIPATORY instead, aimed at BTST/swing trades and catching a big move
early rather than after the fact:

- **🎯 Coiling / NR7** (under Close, display only) — is this stock's
  Bollinger Band width currently near a multi-week low relative to its
  own recent history (the classic Minervini Volatility Contraction
  Pattern), or is today's range the narrowest of the last 7 bars? Tight
  consolidation has historically preceded outsized breakouts more often
  than an already-wide range — but a coiled stock can break either
  direction, so this is a "worth watching" badge, not a directional gate.
- **💥 Big candle / range expansion** — a bar whose own true range is a
  real multiple of its ATR AND whose close lands in the extreme top/bottom
  of its own high-low range (a real range expansion with real conviction,
  not just a wide indecisive bar). The badge shows the level that bar set
  and whether price has since continued through it (✓) — the "does
  yesterday's big candle hold up" read that matters for a BTST/swing
  continuation decision. Turn on "Require big-candle agreement" on the
  Settings page to gate Confirmed status on it.
- **Close@N%** — where today's close landed within its own high-low range
  (100% = closed at the high), independent of range size — the classic
  BTST "closed with conviction" checklist item. Turn on "Require
  strong-close agreement" to gate on it.
- **Deliv N%** — NSE's delivery percentage (real overnight conviction vs.
  intraday churn), pulled from NSE's own public bhavcopy archives (Kite
  Connect has no delivery data at all). **Read this carefully**: it is
  never a same-day-live number — NSE only publishes a session's own
  figure after that session's close, so it's always the most recently
  PUBLISHED reading, shown with its own date. It may also never appear at
  all: NSE is known to block requests from some cloud/datacenter hosts,
  and this app degrades gracefully (delivery data just reads "unavailable"
  everywhere) rather than breaking anything if that happens. Check the
  Settings page for a live status line showing whether it's actually
  getting through from wherever this is deployed.

Big-candle and strong-close are also now selectable parameters on the
[Backtest](#) page (alongside RSI/MACD/EMA-BB/etc.) — the actual way to
empirically check, on your own watchlist history, whether these two
precede bigger moves than the confirmatory indicators do.

## Entry quality: am I early, or am I chasing?

Two additions that don't add a new *signal* so much as grade the one you
already have — both flagged as gaps in `PARAMETER_ANALYSIS_2.md`
(Findings #4 and #5) before this:

**Ext N R** (under Close) is how far price already is past its own VWAP,
measured in ATR units and signed by the row's own direction. Negative
means price is still early or pulled back; a positive number beyond your
configured threshold earns a ⚠ and means you'd be *chasing* a move that's
already run rather than catching it as it turns. Until now those two
situations carried an identical "Confirmed" mark, which for a
catch-it-early strategy is exactly the distinction that matters. Uses
session VWAP intraday and falls back to the anchored VWAP on daily/weekly
bars, so it's never silently blank.

**ATR N%** is the stock's ATR as a percentage of its own price — a
volatility *floor*. A name whose own recent range is tiny structurally
cannot deliver a big move no matter how many parameters line up, and
screening those out is different from what the ADX regime check does
(that reads trend *strength*, not movement *size*). Expressed as a
percentage so one threshold behaves the same on a ₹150 stock and a ₹5,000
one. Note this is deliberately *not* applied to the Coiling badge above —
a coiled stock has low volatility precisely because it's about to expand,
which is the opposite of dead.

Both are display-only by default with opt-in gates on the Settings page,
and both are replayable on the Backtest page so you can measure whether
turning them on actually helps.

## What was removed

Two orphaned parameters were deleted rather than left half-alive, both
long-flagged in the analysis docs:

**`rsi_threshold`** (RSI > 65 / < 35) existed only as a Backtest-page
checkbox with no live equivalent anywhere — so you could tune it, get a
number, and never be able to deploy that combination. Any
"Auto-Weight Parameters" run including it was measuring something
unreachable.

**`RSI_MOMENTUM_BULL`/`RSI_MOMENTUM_BEAR`** in the scalp screener (and the
`rsi_up`/`rsi_dn` cross series they fed) were computed on every single
scan and read by nothing — the scalp RSI vote has always been a plain
above/below-50 state check by design. Dead weight that made it genuinely
unclear from the UI which parameters actually count.

Nothing else was removed. In particular **EMA9-vs-Bollinger-mid was kept
despite being the weakest of the four core parameters** (it's really a
slow moving-average cross, not a Bollinger signal — the genuine
Bollinger-volatility read is now the Coiling badge instead): removing it
would silently change the `aligned` score on every historical journal
entry and invalidate past data. The right way to handle it is to lean on
the **Score** column instead of raw Aligned, since Score already
down-weights it based on measured win rate — and to re-run
"Auto-Weight Parameters" across your full watchlist to get a trustworthy
weight for it.

## Risk management (position sizing &amp; daily limits)

Your own research notes (see `NEXT_HORIZON_RESEARCH.md`) flagged this as
more important to real outcomes than any indicator — so on the Settings
page there's now a **Risk management** card: tell it your real **Account
capital** and a **Risk per trade %** (1-2% is the typical starting point
for F&amp;O), and every row's Close cell gains a **Qty** suggestion —
fixed-fractional position sizing, computed off the ATR stop above, sized
to risk exactly that % of your capital if the stop is hit. The toolbar
also shows a **Risk** pill tracking trades you've logged in the [Signal
Journal](#journal-based-confidence-score) today against your own
**Max daily risk %**, **Max concurrent positions**, and a
sector-concentration flag (2+ open trades in the same NSE sector today).
None of this is enforced — this app places no real orders and has no
visibility into your actual broker account, so nothing here can or does
block you from logging another trade past a limit. It's a suggestion and
a plain-language check-in against discipline you set for yourself, not
automation.

## Risk layer (ATR stop/target)

Every row now shows a small suggested **stop-loss / target** line under its
Close price, sized to that stock's own recent volatility (Average True
Range) instead of a flat percentage — a quiet stock gets a tight stop, a
volatile one gets a wide one, automatically. Hover it for the raw ATR
value. This is **display only** — nothing here places an order, and it
never affects whether a row counts as Confirmed. Tune the ATR length and
the stop/target multipliers on the **Settings** page (default 1.5x/3.0x
ATR = a 1:2 risk-reward starting point).

---

## Part 1 — Get Kite Connect API access

1. Go to [developers.kite.trade](https://developers.kite.trade) and sign
   in with your Zerodha account.
2. Subscribe to the **Connect** plan — ₹500/month, this covers the market
   data (historical + live candles) the Scanner needs. Order-placing APIs
   are free, but this app only reads prices, it doesn't place trades.
3. Create a new app. You'll be asked for a **Redirect URL** — for now, set
   it to `http://localhost:5000/kite/callback`. You'll update this once
   you have a real server address (Part 2).
4. Copy your **API Key** and **API Secret** — you'll need them next.

## Part 2 — Get a server to run this on

This needs to be online continuously during market hours, so it can't be
your personal laptop unless you're willing to leave it on and connected
all day. Two ways to get one:

### Option A — Render (fastest, I can set this up with you)

Render is a cloud host that a Claude session can provision directly
through its MCP connector, instead of you clicking through a dashboard
yourself. The Starter plan (**$7/month**, always-on — the free tier
sleeps after 15 minutes idle, which would break the background scanner)
is the cheapest tier that works for this app.

To use this path: connect the **Render** integration from your Claude
settings (Settings → Connectors → add Render, or ask me and I'll surface
the connect prompt). Once it's connected, tell me to go ahead and I'll:
create the web service from this project's code, set your environment
variables (you'll still enter `KITE_API_KEY`/`KITE_API_SECRET`/
`DASHBOARD_PASSWORD`/etc. yourself so I never see your credentials), and
give you the live URL to use as your Redirect URL in Part 1, step 3.
I'll always confirm with you before anything that spends money — this
plan does have a real $7/month charge on your Render account.

### Option B — A small VPS (DigitalOcean / Hetzner / AWS Lightsail)

1. Sign up with a provider — budget ~$4-6/month for the smallest box.
2. Create a server with **Ubuntu 22.04** (any recent Ubuntu works).
3. Note its public IP address — you'll use this to reach your dashboard
   and as part of your Redirect URL.
4. Update the Redirect URL in your Kite Connect app settings (Part 1,
   step 3) to `http://YOUR_SERVER_IP:5000/kite/callback`.

*(If you'd rather I walk you through one specific provider's exact
click-by-click setup, tell me which one and I'll write that out.)*

## Part 3 — Deploy the app

SSH into your server, then:

```bash
sudo apt update && sudo apt install -y python3-pip python3-venv

# Upload this project folder to the server (scp, git, or however you prefer),
# then from inside the project folder:
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
nano .env   # fill in KITE_API_KEY, KITE_API_SECRET, REDIRECT_URL (with your
            # server's real IP), and DASHBOARD_PASSWORD (make one up - long
            # and random, this is what keeps your dashboard private).
            # ANTHROPIC_API_KEY is optional - only needed for AI Insights.
```

Run it once to make sure it starts cleanly:

```bash
python run.py
```

Open `http://YOUR_SERVER_IP:5000` in a browser — you should see a login
prompt (that's the DASHBOARD_PASSWORD you set, not your Zerodha one), then
the Scanner page with a "Login to Kite" button. Stop it with Ctrl+C once
confirmed working.

### Keep it running permanently

Running `python run.py` stops the moment you close your SSH session. Use
`systemd` so it survives reboots and restarts automatically if it crashes:

```bash
sudo tee /etc/systemd/system/scanner.service > /dev/null <<EOF
[Unit]
Description=Scanner Dashboard
After=network.target

[Service]
WorkingDirectory=$(pwd)
ExecStart=$(pwd)/venv/bin/gunicorn -w 1 -b 0.0.0.0:5000 run:app
Restart=always
User=$(whoami)

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now scanner
```

Check it's running: `sudo systemctl status scanner`. To see logs:
`journalctl -u scanner -f`.

## Your daily routine

1. Open `http://YOUR_SERVER_IP:5000` (bookmark it).
2. Enter your dashboard password (once per browser session, not per day).
3. Click **"Login to Kite"**, sign in with your Zerodha credentials + 2FA.
4. You're done — the dashboard now scans your watchlist automatically,
   shows current signals, live charts (click any row), and an AI summary
   until the token expires overnight, at which point tomorrow's visit
   starts back at step 3.

First time only: open **Settings**, click **"Load current F&O list from
Kite"** (after logging in) to populate your watchlist with the live,
exact F&O-eligible stock list, and set your preferred timeframe/
parameters. Everything there applies immediately — no restart.

## Configuration

Almost everything now lives in the **Settings** page in the browser, not
`.env` — watchlist, timeframe (including 4-hour), MACD preset, RSI/EMA/BB
lengths, minimum indicators required (2-of-3 or 3-of-3), and scan
frequency. `.env` only holds secrets and one-time setup values:
`KITE_API_KEY`, `KITE_API_SECRET`, `REDIRECT_URL`, `DASHBOARD_PASSWORD`,
the optional `ANTHROPIC_API_KEY` for AI Insights, `TELEGRAM_BOT_TOKEN`/
`TELEGRAM_CHAT_ID` for Telegram alerts, and the optional
`MARKETAUX_API_TOKEN` for the News feature (see Alerts/News above for
both). Settings changes
are saved to `scanner_settings.json` next to the app, so they survive a
restart too.

## Known limitations, please read

- **This is not investment advice.** Signals are based on historical
  price patterns and the backtest we ran earlier showed a fairly low win
  rate (see the earlier report) — treat this as one input, not a trading
  system to follow blindly. The AI Insights panel describes the scan
  data, it does not add new analysis beyond what's in the numbers.
- **No order-placement logic, still** — the app now suggests a
  stop-loss/target (ATR-based) and a position-size (fixed-fractional,
  see below), but these are display-only suggestions computed from
  numbers you configured; it still doesn't place trades or know
  anything about your real broker account/positions/P&L. That's
  intentional.
- **Single point of failure**: if your server goes down, or you forget
  to log in one morning, you get no signals (and no alerts) that day —
  there's no separate uptime monitor watching the app itself. Telegram
  alerts (see above) at least mean you don't have to keep the dashboard
  open, but if the server is down, nothing fires.
- **Rate limits**: Kite Connect has API rate limits; the default 3-minute
  scan interval is chosen to stay comfortably under them with a normal
  F&O-sized watchlist (~180-200 stocks as of writing). If you load the
  full live F&O list and see rate-limit errors in the dashboard's warning
  banner, raise `SCAN_INTERVAL_SECONDS` in Settings.
- **AI Insights costs money per call** (your own Anthropic API usage,
  typically a fraction of a cent per summary) and is capped at roughly
  one call per scan interval via caching — but it's still real usage on
  your key, so keep an eye on it if you set a very short scan interval.
- **Chart data depth is limited by Kite's historical-data API limits**
  per interval (shorter timeframes get less lookback) — this is a Kite
  platform limit, not something this app can work around.
