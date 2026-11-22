# NSE Paper Autopilot: Day Trading for Indian Markets

**A paper-trading autopilot for the Indian stock market: NSE, Nifty 100, intraday opening-range breakouts.**
Cron starts it every NSE trading day. It finds breakouts, checks the odds by replaying the setup 10,000 times,
sizes the position to your rupee limits, paper-trades it, and reports on Telegram and a self-updating HTML status
page.

> **Paper trading only. Not financial advice.** No broker is connected and no real order is ever placed.
> It is a tool for testing a strategy honestly before risking money.

| | |
|---|---|
| Market | **India: NSE (National Stock Exchange)** |
| Universe | **Nifty 100**, Yahoo tickers like `RELIANCE.NS` |
| Hours | NSE session 09:15 to 15:30 **IST**. New entries 09:30 to 14:45; every trade closed by 15:10 |
| Holidays | NSE holiday calendar |
| Money | **Indian rupees (₹)**. Charges modelled on **Zerodha** equity intraday: brokerage, STT, exchange, SEBI, stamp duty, GST |
| Data | Free Yahoo Finance 5-minute bars (may be delayed; fine for paper, not for live trading) |
| Dependencies | **None.** Python 3 standard library only |

## How a trading day runs (IST)

| Time | What happens |
|---|---|
| 09:25 | Cron starts the run. On an NSE holiday it logs "not a trading day" and exits |
| 09:35 | The 15-minute opening range is set |
| Every 5 min until 14:45 | Scan the Nifty 100 for opening-range breakouts. Each new breakout gets: entry check → stop at the range midpoint and a 1R target → **Time Machine** (10,000 simulated sessions from the stock's own last 60 days, plus every past day the setup fired) |
| Gate | Take the trade only if *target first* beats *stop first* by at least **5 points** and the same rule averaged a **positive R** over at least 10 past triggers. The best edge wins. Then the budget sizes it and the risk check must PASS |
| Paper fill | At the trigger bar's close. Every 5 minutes: stop, target, or flat at 15:10 |
| After an exit | Back to watching, **one position at a time, up to `trades_per_day`**. It stops early once the day's net is down to the daily loss limit minus one trade's max loss |
| End of day | One journal row per trade (or why there was none), and a Telegram summary |

Breakouts that fired while a trade was still open are **skipped as stale** rather than filled at an old price.

## Layout

```
day-trader-skill/       # the upstream skill pack (MIT, Jake Schincariol). Only autopilot.py is changed (multi-trade)
  autopilot.py          # the day loop: run (cron), replay (offline), telegram (setup test)
  trader-*/             # scanner, entry, exit, time machine, budget, risk, alerts, news, analyst, broker, pelosi
  tests/                # 319 unit tests
tools/
  status_page.py        # builds a single-file HTML status page (light/dark, mobile)
  ap_harness.py         # scripted multi-trade test of the autopilot's day loop
```

## Setup (Linux server, Python 3.10+)

```bash
git clone https://github.com/<you>/nse-paper-autopilot.git
cd nse-paper-autopilot/day-trader-skill

# 1. Tests: run them BEFORE creating trader/ (an India config switches the US-written tests into NSE mode)
python3 -I -m unittest discover -s tests          # expect: Ran 319 tests ... OK

# 2. India config and the Nifty 100 universe
mkdir -p trader && cp trader-budget/config.example-in.json trader/config.json
echo '{"trades_per_day": 3}' > trader/autopilot.json
python3 -I trader-scanner/scan.py universe        # "Kept 100 NIFTY100 stocks"

# 3. Prove it runs: an offline replay of a past day (do it in a throwaway copy to keep trader/ clean)
python3 -I autopilot.py replay --date 2026-10-15 --offline tests/fixtures/market-in/DEMO.NS-60d5m.json.gz
```

**Telegram (optional).** Create a bot with @BotFather, then:
```bash
echo '{"token": "<your bot token>"}' > trader/telegram.json && chmod 600 trader/telegram.json
# send "hi" to your bot, then:
python3 -I autopilot.py telegram                  # "test message sent"
```
Without `telegram.json`, messages are only written to the log.

**Cron** (server clock in UTC; `55 3` UTC = 09:25 IST, Monday to Friday):
```
55 3 * * 1-5 cd /path/to/day-trader-skill && /usr/bin/python3 -I autopilot.py run >> trader/autopilot.log 2>&1
*/5 * * * * /usr/bin/python3 -I /path/to/tools/status_page.py --home /path/to/day-trader-skill --out /var/www/somewhere/autopilot.html
```
If a CDN caches the page, that's fine: the page fetches a fresh copy of itself with a unique query string on load
and every 2 minutes. The page has no password, so give it an unguessable file name or put it behind auth.

## Settings

`trader/config.json` (the risk limits; amounts are **rupees** in NSE mode even though the keys end in `_usd`):
`budget`, `max_loss_per_trade_usd`, `max_daily_loss_usd`, `max_trades_per_day`, `max_position_pct`,
`cash_buffer_pct`, `min_reward_risk`, `no_entries_before`, `no_entries_after`, `flat_by`, `"market": "IN"`.

`trader/autopilot.json` (all optional): `trades_per_day` (1), `min_edge_pts` (5), `min_history_triggers` (10),
`require_positive_history` (true), `target_r` ("1R"), `max_candidates` (10), `first_scan` ("09:35"),
`last_entry` ("14:45"), `flat_by` ("15:10"), `every_minutes` (5).

## Testing the multi-trade loop

The bundled market data has only one stock, so offline replays can never show more than one trade in a day.
`tools/ap_harness.py` runs the real day loop against scripted breakouts and exits instead:
```bash
cp -r day-trader-skill /tmp/ap-test && rm -f /tmp/ap-test/trader/telegram.json   # so it can't message you
mkdir -p /tmp/ap-test/trader && cp day-trader-skill/trader-budget/config.example-in.json /tmp/ap-test/trader/config.json
echo '{"trades_per_day": 3}' > /tmp/ap-test/trader/autopilot.json
python3 -I tools/ap_harness.py /tmp/ap-test
```
It checks: three trades with a stale breakout skipped, the stop at the loss cut-off, a no-trade day, and the stop
at the last entry time.

## Credits and licence

Built on [**day-trader-skill**](https://github.com/Jakeschincariol/day-trader-skill) by Jake Schincariol (MIT),
which supplies the scanner, entry, exit, time machine, budget and risk tools and the NSE support. The changes here
(the multi-trade autopilot, the status page and the harness) are released under the same MIT licence. See
`LICENSE`.

**Disclaimer:** for education and paper trading only. Past or simulated results do not predict future returns.
Intraday trading carries a high risk of loss. Talk to a SEBI-registered adviser before trading real money.
