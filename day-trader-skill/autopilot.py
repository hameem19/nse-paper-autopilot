#!/usr/bin/env python3
"""Autopilot: PAPER day trades on NSE (up to trades_per_day, one open at a time), run by cron,
reported on Telegram. After an exit it goes back to watching until last_entry, unless the day's
trade limit is used or the day's net is down to max_daily_loss_usd minus max_loss_per_trade_usd
(trader/config.json). A breakout only counts while it is fresh (trigger bar within 2 scan cycles).

It only chains the pack's own tools (scanner -> entry -> exit -> time machine
-> budget -> risk) and keeps the paper position itself. It never sends an
order anywhere: there is no broker call in this file. Standard library only.

    python3 autopilot.py run                  # cron, trading days, starts 9:25 IST
    python3 autopilot.py telegram             # after you message your bot once: finds your chat id, sends a test
    python3 autopilot.py replay --date 2026-10-19 --offline tests/fixtures/market-in/DEMO.NS-60d5m.json.gz

The day, in IST (times from trader/autopilot.json):
  09:35  the 15-minute opening range is set. Every 5 minutes until 14:45 the
         ORB scan runs on trader/universe.txt; each NEW breakout (up to
         max_candidates) gets an entry check, an exit plan (stop at the range
         midpoint, target_r) and a time machine run.
  gate   take it only if the replays say target first beats stop first by at
         least min_edge_pts points AND the same rule on past sessions averaged
         a positive R over at least min_history_triggers triggers. The best
         edge wins. Then trader-budget sizes it and trader-risk must PASS.
  paper  filled at the trigger bar's close. Every 5 minutes the new bars are
         checked: a bar that opens through the stop or target fills at its
         open, one that touches the stop fills at the stop, one that touches
         the target fills at the target, a bar that touches both counts as the
         stop. Flat at flat_by at that bar's close. Charges are Zerodha's.
  report trader/journal.csv gets one row a day (a trade or why not), and
         Telegram gets: the entry, the exit, the day's summary.

trader/telegram.json: {"token": "...", "chat_id": "..."} (chmod 600). Without
it messages are only printed (the cron log).
"""

import argparse
import csv
import datetime as dt
import fcntl
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request

HOME = os.path.dirname(os.path.abspath(__file__))
TRADER = os.path.join(HOME, "trader")
STATE_DIR = os.path.join(TRADER, "state")
IST = dt.timedelta(hours=5, minutes=30)
DEFAULTS = {"first_scan": "09:35", "last_entry": "14:45", "flat_by": "15:10", "every_minutes": 5,
            "settle_seconds": 90, "target_r": "1R", "min_edge_pts": 5, "min_history_triggers": 10,
            "max_candidates": 10, "trades_per_day": 1, "require_positive_history": True}
JOURNAL_COLS = ["date", "status", "symbol", "entry_time", "entry", "stop", "target", "qty", "edge_pts",
                "history_avg_r", "exit_time", "exit", "exit_reason", "gross", "charges", "net", "r", "note"]


# ---------------------------------------------------------------- small helpers

def utcnow():
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None, microsecond=0)


def ist(utc):
    return utc + IST


def hhmm(t):
    return dt.time(int(t[:2]), int(t[3:5]))


def at(day, t):
    """IST date + "HH:MM" -> naive UTC."""
    return dt.datetime.combine(day, hhmm(t)) - IST


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write_json(path, doc):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
    os.replace(tmp, path)


def load_module(folder, name):
    spec = importlib.util.spec_from_file_location("ap_" + name, os.path.join(HOME, folder, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    out, err = sys.stdout, sys.stderr
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.stdout, sys.stderr = out, err    # the tools localize their own output; not ours
    return mod


def log(msg):
    print("%s %s" % (ist(utcnow()).strftime("%Y-%m-%d %H:%M:%S IST"), msg), flush=True)


# ---------------------------------------------------------------- telegram

def telegram(text, cfg_path=os.path.join(TRADER, "telegram.json")):
    log("telegram: " + text.replace("\n", " | "))
    tg = read_json(cfg_path) or {}
    if not tg.get("token") or not tg.get("chat_id"):
        return False
    data = urllib.parse.urlencode({"chat_id": tg["chat_id"], "text": text}).encode()
    try:
        urllib.request.urlopen("https://api.telegram.org/bot%s/sendMessage" % tg["token"], data, timeout=20).read()
        return True
    except Exception as e:  # a message that fails must never stop the day
        log("telegram failed: %s" % e)
        return False


def cmd_telegram(a):
    path = os.path.join(TRADER, "telegram.json")
    tg = read_json(path) or {}
    if not tg.get("token"):
        print("Put your bot token in %s first: {\"token\": \"...\"}" % path)
        return 1
    with urllib.request.urlopen("https://api.telegram.org/bot%s/getUpdates" % tg["token"], timeout=20) as r:
        ups = json.load(r).get("result", [])
    chats = [u["message"]["chat"]["id"] for u in ups if "message" in u]
    if not chats:
        print("No messages yet: open your bot in Telegram, send it 'hi', then run this again.")
        return 1
    tg["chat_id"] = str(chats[-1])
    write_json(path, tg)
    os.chmod(path, 0o600)
    ok = telegram("Autopilot connected. Paper trades only: nothing is ever sent to a broker.")
    print("chat id %s saved; test message %s." % (tg["chat_id"], "sent" if ok else "FAILED"))
    return 0 if ok else 1


# ---------------------------------------------------------------- the tools

class Clock(object):
    """Live: the real clock and real sleeps. Replay: a simulated clock over a saved chart."""

    def __init__(self, replay_day=None, offline=None):
        self.offline = offline
        self.now = at(replay_day, "09:25") if replay_day else None

    def utc(self):
        return self.now or utcnow()

    def sleep_until(self, utc):
        if self.now is not None:
            self.now = max(self.now, utc)
            return
        while utcnow() < utc:
            time.sleep(min(60, max(1, (utc - utcnow()).total_seconds())))

    def stamp(self):
        return ist(self.utc()).strftime("%Y-%m-%d %H:%M")


def tool(clock, script, *args):
    """Run one of the pack's scripts at the clock's time. -> (exit code, text)."""
    cmd = [sys.executable, "-I", os.path.join(HOME, script)] + list(args) + ["--now", clock.stamp()]
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    p = subprocess.run(cmd, cwd=HOME, env=env, capture_output=True, text=True, encoding="utf-8", timeout=600)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def bars_today(clock, sym, day):
    """[(start IST datetime, o, h, l, c)] of today's COMPLETED 5-minute bars."""
    if clock.offline:
        import gzip
        op = gzip.open if clock.offline.endswith(".gz") else open
        with op(clock.offline, "rt", encoding="utf-8") as fh:
            doc = json.load(fh)
    else:
        url = ("https://query2.finance.yahoo.com/v8/finance/chart/%s?range=1d&interval=5m"
               % urllib.parse.quote(sym))
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            doc = json.load(r)
    res = doc["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    now_ist = ist(clock.utc())
    out = []
    for i, ts in enumerate(res.get("timestamp") or []):
        start = ist(dt.datetime(1970, 1, 1) + dt.timedelta(seconds=ts))
        if start.date() != day or ts % 300 or start + dt.timedelta(minutes=5) > now_ist:
            continue
        o, h, l, c = (q[k][i] for k in ("open", "high", "low", "close"))
        if None not in (o, h, l, c):
            out.append((start, o, h, l, c))
    return out


# ---------------------------------------------------------------- the day

def evaluate(clock, sym, cfg, work):
    """entry -> exit -> time machine for one breakout. -> candidate dict or None (with why)."""
    off = ["--offline", clock.offline] if clock.offline else []
    e, p, t = (os.path.join(work, "%s-%s.json" % (sym, k)) for k in ("entry", "plan", "tm"))
    code, out = tool(clock, "trader-entry/entry.py", "check", sym, "--write", e, *off)
    entry = read_json(e)
    if code != 0 or not entry or entry.get("status") != "triggered":
        return None, "%s: entry did not confirm" % sym
    code, out = tool(clock, "trader-exit/exit.py", "plan", "--from", e, "--target", cfg["target_r"],
                     "--flat-by", cfg["flat_by"], "--write", p, *off)
    if code != 0 or not read_json(p):
        return None, "%s: no exit plan (%s)" % (sym, out.strip().splitlines()[-1] if out.strip() else "?")
    code, out = tool(clock, "trader-timemachine/timemachine.py", "run", "--plan", p, "--until", cfg["flat_by"],
                     "--write", t, *off)
    tm = read_json(t)
    if code != 0 or not tm:
        return None, "%s: time machine failed (%s)" % (sym, out.strip().splitlines()[-1] if out.strip() else "?")
    mc, hist = tm["mc"], tm["history"]
    edge = round(100 * (mc["p_target"] - mc["p_stop"]), 1)
    c = {"symbol": sym, "plan_path": p, "plan": read_json(p), "edge": edge, "p_target": mc["p_target"],
         "p_stop": mc["p_stop"], "hist_avg_r": hist.get("avg_R"), "hist_n": hist.get("triggered", 0)}
    if edge < cfg["min_edge_pts"]:
        return None, "%s: edge %+.1f pts, needs %+d" % (sym, edge, cfg["min_edge_pts"])
    if (hist.get("triggered") or 0) < cfg["min_history_triggers"] or (
            cfg["require_positive_history"] and (hist.get("avg_R") or 0) <= 0):
        return None, "%s: history %s triggers, avg R %s" % (sym, hist.get("triggered"), hist.get("avg_R"))
    return c, None


def open_trade(clock, c, cfg, size_mod):
    shutil.copyfile(c["plan_path"], os.path.join(TRADER, "plan.json"))
    code, out = tool(clock, "trader-budget/size.py", "--write")
    orders = (read_json(os.path.join(TRADER, "orders.json")) or {}).get("orders") or []
    if code != 0 or not orders:
        return None, "%s: sizing refused (%s)" % (c["symbol"], out.strip().splitlines()[-1] if out.strip() else "?")
    code, out = tool(clock, "trader-risk/check.py", "--json")
    try:
        verdict = json.loads(out[out.index("{"):])["results"][0]
    except (ValueError, KeyError, IndexError):
        verdict = {"verdict": "BLOCK", "reasons": ["risk check output unreadable"]}
    if verdict.get("verdict") != "PASS":
        return None, "%s: risk BLOCK (%s)" % (c["symbol"], "; ".join(verdict.get("reasons") or []))
    o, pl = orders[0], c["plan"]
    return {"symbol": c["symbol"], "qty": int(o["qty"]), "entry": pl["entry"], "stop": pl["stop"],
            "target": pl["target"], "entry_time": pl.get("entry_time"), "risk": pl["risk_per_share"],
            "edge": c["edge"], "hist_avg_r": c["hist_avg_r"], "flat_by": cfg["flat_by"]}, None


def manage(clock, pos, day, size_mod):
    """New completed bars since the last check -> the exit, or None while it is still open."""
    bars = bars_today(clock, pos["symbol"], day)
    last = pos.get("checked_through")
    flat = dt.datetime.combine(day, hhmm(pos["flat_by"]))
    entry_end = dt.datetime.combine(day, dt.datetime.strptime(pos["entry_time"], "%I:%M %p").time())
    for start, o, h, l, c in bars:
        end = start + dt.timedelta(minutes=5)
        if end <= entry_end or (last and start.strftime("%H:%M") <= last):
            continue
        pos["checked_through"] = start.strftime("%H:%M")
        s, t = pos["stop"], pos["target"]
        if o <= s:
            return o, end, "gapped through the stop"
        if l <= s:
            return s, end, "stop"
        if o >= t:
            return o, end, "opened above the target"
        if h >= t:
            return t, end, "target"
        if end >= flat:
            return c, end, "flat by %s" % pos["flat_by"]
    if ist(clock.utc()) >= flat + dt.timedelta(minutes=10) and bars:   # the flat-by bar never came
        return bars[-1][4], flat, "flat by %s (last price, data stopped at %s)" % (
            pos["flat_by"], bars[-1][0].strftime("%H:%M"))
    return None


def close_trade(pos, px, when, reason, size_mod):
    gross = round((px - pos["entry"]) * pos["qty"], 2)
    charges = size_mod.round_trip_charges(pos["qty"], pos["entry"], px)
    net = round(gross - charges, 2)
    r = round((px - pos["entry"]) / pos["risk"], 2)
    pos.update(exit=round(px, 2), exit_time=when.strftime("%H:%M"), exit_reason=reason, gross=gross,
               charges=charges, net=net, r=r)
    return pos


def journal(row):
    path = os.path.join(TRADER, "journal.csv")
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=JOURNAL_COLS, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow(row)


def run_day(clock, cfg):
    entry_mod = load_module("trader-entry", "entry")
    size_mod = load_module("trader-budget", "size")
    if entry_mod.MARKET != "IN":
        raise SystemExit('trader/config.json needs "market": "IN"')
    day = ist(clock.utc()).date()
    ok, why = entry_mod.trading_day(day)
    if not ok:
        log("not a trading day (%s)" % why)
        return 0
    state_path = os.path.join(STATE_DIR, "autopilot-%s.json" % day.isoformat())
    st = read_json(state_path) or {"date": day.isoformat(), "status": "watching", "seen": [], "skips": [],
                                   "trades": 0, "closed": []}
    if st["status"] == "done":
        log("already done today")
        return 0
    st.setdefault("closed", [])
    if st["status"] == "closed" and st.get("position") and not st["closed"]:  # a state file from the 1-trade version
        st["closed"].append(st.pop("position"))
    # Stop new entries once the day's net is down this far, so one more full loss still fits the daily limit.
    risk_cfg = read_json(os.path.join(TRADER, "config.json")) or {}
    daily, per_trade = risk_cfg.get("max_daily_loss_usd"), risk_cfg.get("max_loss_per_trade_usd")
    loss_stop = float(daily) - float(per_trade) if daily and per_trade else None
    work = os.path.join(STATE_DIR, "autopilot-%s" % day.isoformat())
    os.makedirs(work, exist_ok=True)
    step = dt.timedelta(minutes=cfg["every_minutes"])
    settle = dt.timedelta(seconds=cfg["settle_seconds"])
    tick = at(day, cfg["first_scan"])
    last_entry, flat = at(day, cfg["last_entry"]), at(day, cfg["flat_by"])
    if st["status"] == "watching" and not st["trades"]:
        telegram("Autopilot %s: watching the Nifty 100 for opening-range breakouts (paper, up to %d trades)."
                 % (day.isoformat(), cfg["trades_per_day"]))
    while True:
        clock.sleep_until(tick + settle)
        try:
            if st["status"] == "watching" and tick < last_entry and st["trades"] < cfg["trades_per_day"]:
                watch_step(clock, cfg, st, work, size_mod)
            elif st["status"] == "open":
                ex = manage(clock, st["position"], day, size_mod)
                if ex:
                    pos = close_trade(st.pop("position"), ex[0], ex[1], ex[2], size_mod)
                    st["closed"].append(pos)
                    day_net = sum(p["net"] for p in st["closed"])
                    telegram("PAPER EXIT %s: %s at %.2f at %s IST. %s%.0f net after %.0f charges (%+.2fR). "
                             "Day so far: %s%.0f."
                             % (pos["symbol"], pos["exit_reason"], pos["exit"], pos["exit_time"],
                                "+" if pos["net"] >= 0 else "-", abs(pos["net"]), pos["charges"], pos["r"],
                                "+" if day_net >= 0 else "-", abs(day_net)))
                    if st["trades"] >= cfg["trades_per_day"]:
                        st.update(status="closed", stop_reason="%d trades, the day's limit" % st["trades"])
                    elif loss_stop is not None and day_net <= -loss_stop:
                        st.update(status="closed", stop_reason="day net %.0f reached the %.0f loss cut-off"
                                  % (day_net, -loss_stop))
                    elif tick + step >= last_entry:
                        st.update(status="closed", stop_reason="past the last entry time %s" % cfg["last_entry"])
                    else:
                        st.update(status="watching")
                        log("back to watching for breakouts (%d of %d trades used)"
                            % (st["trades"], cfg["trades_per_day"]))
        except Exception as e:  # one bad cycle (Yahoo down) must not kill the day
            log("cycle %s failed: %s" % (ist(tick).strftime("%H:%M"), e))
        write_json(state_path, st)
        if st["status"] == "closed" or (st["status"] == "watching" and (tick + step >= last_entry or
                                                                        st["trades"] >= cfg["trades_per_day"])):
            break
        if tick > flat + step:
            break
        tick += step
    finish(day, st, state_path)
    return 0


def watch_step(clock, cfg, st, work, size_mod):
    args = ["orb", "--json", "--no-write", "--quiet"] + (["--offline", clock.offline] if clock.offline else [])
    code, out = tool(clock, "trader-scanner/scan.py", *args)
    try:
        hits = json.loads(out[out.index("{"):]).get("hits") or []
    except ValueError:
        log("scan: " + (out.strip().splitlines()[-1] if out.strip() else "no output"))
        return
    fresh = [h["symbol"] for h in hits if h["symbol"] not in st["seen"]][:cfg["max_candidates"]]
    st["seen"] += fresh
    if not fresh:
        return
    log("new breakouts: " + ", ".join(fresh))
    best = []
    max_age = 2 * dt.timedelta(minutes=cfg["every_minutes"]) + dt.timedelta(seconds=cfg["settle_seconds"])
    now_ist = ist(clock.utc())
    for sym in fresh:
        c, why = evaluate(clock, sym, cfg, work)
        if c:
            # A paper fill is at the trigger bar's close, so a breakout that fired long ago (e.g. while an
            # earlier trade was open) would be filled at a price that is no longer there: skip it.
            try:
                trig = dt.datetime.combine(now_ist.date(), dt.datetime.strptime(
                    c["plan"].get("entry_time") or "", "%I:%M %p").time())
            except ValueError:
                trig = None
            if trig is not None and now_ist - trig > max_age:
                c, why = None, "%s: breakout at %s is stale" % (sym, trig.strftime("%H:%M"))
        if c:
            best.append(c)
        else:
            st["skips"].append(why)
    for c in sorted(best, key=lambda x: -x["edge"]):
        pos, why = open_trade(clock, c, cfg, size_mod)
        if not pos:
            st["skips"].append(why)
            continue
        st.update(status="open", position=pos, trades=st["trades"] + 1)
        telegram("PAPER ENTRY %s: %d shares at %.2f (%s IST). Stop %.2f, target %.2f, flat by %s. "
                 "Odds: target first %.0f%% vs stop first %.0f%%; past avg %+.2fR. Max loss about %.0f."
                 % (pos["symbol"], pos["qty"], pos["entry"], pos["entry_time"], pos["stop"], pos["target"],
                    pos["flat_by"], 100 * c["p_target"], 100 * c["p_stop"], c["hist_avg_r"],
                    pos["qty"] * pos["risk"]))
        return


def finish(day, st, state_path):
    closed = st.get("closed") or []
    pos = st.get("position") if st["status"] == "open" else None
    sign = lambda x: "+" if x >= 0 else "-"
    for p in closed:
        journal(dict(p, date=day.isoformat(), status="trade", edge_pts=p["edge"],
                     history_avg_r=p["hist_avg_r"], note=""))
    if pos:
        journal(dict(pos, date=day.isoformat(), status="error", note="still open at the end of the run"))
    if closed:
        net = sum(p["net"] for p in closed)
        trades = ", ".join("%s %s%.0f (%+.2fR)" % (p["symbol"], sign(p["net"]), abs(p["net"]), p["r"])
                           for p in closed)
        summary = ("Day %s: %d paper trade%s, net %s%.0f. %s."
                   % (day, len(closed), "" if len(closed) == 1 else "s", sign(net), abs(net), trades))
        if st.get("stop_reason"):
            summary += " Stopped: %s." % st["stop_reason"]
    elif not pos:
        why = "; ".join(st["skips"][-5:]) if st["skips"] else "no opening-range breakouts"
        journal({"date": day.isoformat(), "status": "no trade", "note": why})
        summary = "Day %s: no paper trade. Breakouts looked at: %d. %s" % (day, len(st["seen"]), why)
    else:
        summary = "Day %s:" % day
    if pos:
        summary += " The paper trade in %s was still open when the run ended. Check the log." % pos["symbol"]
    st["status"] = "done"
    write_json(state_path, st)
    telegram(summary)


# ---------------------------------------------------------------- main

def config():
    cfg = dict(DEFAULTS)
    cfg.update(read_json(os.path.join(TRADER, "autopilot.json")) or {})
    return cfg


def main(argv=None):
    ap = argparse.ArgumentParser(description="Paper-trading autopilot for the day-trader pack (NSE).")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("run", help="today's session, live (cron)")
    sub.add_parser("telegram", help="find your chat id and send a test message")
    p = sub.add_parser("replay", help="a past session from a saved chart, on a simulated clock")
    p.add_argument("--date", required=True)
    p.add_argument("--offline", required=True)
    a = ap.parse_args(argv)
    os.chdir(HOME)
    if a.cmd == "telegram":
        return cmd_telegram(a)
    if a.cmd not in ("run", "replay"):
        ap.print_help()
        return 1
    os.makedirs(STATE_DIR, exist_ok=True)
    lock = open(os.path.join(STATE_DIR, "autopilot.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("another autopilot is running")
        return 1
    clock = Clock(dt.date.fromisoformat(a.date), os.path.abspath(a.offline)) if a.cmd == "replay" else Clock()
    try:
        return run_day(clock, config())
    except Exception as e:
        telegram("Autopilot crashed: %s" % e)
        raise


if __name__ == "__main__":
    sys.exit(main())
