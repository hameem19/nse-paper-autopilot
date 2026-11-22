"""Builds tests/fixtures/market-in/DEMO.NS-60d5m.json.gz, a SYNTHETIC NSE chart.

It is TSLA's real 60 days of 5-minute bars (the trader-timemachine fixture)
moved onto the NSE clock: each session's 9:30 ET bar becomes the 9:15 IST bar,
the last three bars of each US session are dropped (NSE has 75 bars, not 78),
sessions on NSE holidays are dropped, and prices are x4. Only for testing the
IN clock; it says nothing about any Indian stock.

    python3 tests/make_in_fixture.py
"""

import datetime
import gzip
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "tests", "fixtures", "trader-timemachine", "TSLA-60d5m.json.gz")
OUT = os.path.join(ROOT, "tests", "fixtures", "market-in", "DEMO.NS-60d5m.json.gz")
NSE_CLOSED = {"2026-09-14", "2026-10-02", "2026-10-20"}
EPOCH = datetime.datetime(1970, 1, 1)
SCALE = 4.0


def main():
    with gzip.open(SRC, "rt", encoding="utf-8") as fh:
        doc = json.load(fh)
    res = doc["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    days = {}
    for i, ts in enumerate(res["timestamp"]):
        utc = EPOCH + datetime.timedelta(seconds=ts)
        days.setdefault((utc - datetime.timedelta(hours=10)).date(), []).append(i)  # US session day
    ts_out, cols = [], {k: [] for k in ("open", "high", "low", "close", "volume")}
    for day in sorted(days):
        if day.isoformat() in NSE_CLOSED:
            continue
        idx = sorted(days[day], key=lambda i: res["timestamp"][i])
        first = res["timestamp"][idx[0]]
        start = int((datetime.datetime.combine(day, datetime.time(3, 45)) - EPOCH).total_seconds())  # 9:15 IST
        for i in idx:
            k = (res["timestamp"][i] - first) // 300
            if k >= 75 or (res["timestamp"][i] - first) % 300:
                continue
            ts_out.append(start + k * 300)
            for c in cols:
                v = q[c][i]
                cols[c].append(None if v is None else (v if c == "volume" else round(v * SCALE, 2)))
    meta = dict(res["meta"], symbol="DEMO.NS", currency="INR", exchangeName="NSI", fullExchangeName="NSE",
                gmtoffset=19800, timezone="IST", exchangeTimezoneName="Asia/Kolkata",
                regularMarketTime=ts_out[-1] + 300, longName="Synthetic demo", shortName="Synthetic demo")
    for k in ("regularMarketPrice", "fulldayPrice", "regularMarketDayHigh", "regularMarketDayLow",
              "chartPreviousClose", "previousClose", "fiftyTwoWeekHigh", "fiftyTwoWeekLow"):
        if isinstance(meta.get(k), (int, float)):
            meta[k] = round(meta[k] * SCALE, 2)
    meta.pop("currentTradingPeriod", None)
    meta.pop("tradingPeriods", None)
    out = {"chart": {"result": [{"meta": meta, "timestamp": ts_out, "indicators": {"quote": [cols]}}], "error": None}}
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with gzip.open(OUT, "wt", encoding="utf-8") as fh:
        json.dump(out, fh, separators=(",", ":"))
    print("wrote %s: %d bars" % (OUT, len(ts_out)))


if __name__ == "__main__":
    main()
