"""Drive autopilot.run_day with scripted breakouts/exits to test the multi-trade state machine.

usage: python3 -I ap_harness.py /path/to/copy
Stubs: the scanner call, evaluate, open_trade, manage, telegram. Real: run_day, watch_step (incl. the
freshness rule), close_trade (real Zerodha charges), finish, journal, state files, loss cut-off.
"""
import datetime as dt
import importlib.util
import json
import os
import sys

ROOT = sys.argv[1]
spec = importlib.util.spec_from_file_location("ap", os.path.join(ROOT, "autopilot.py"))
ap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ap)
os.chdir(ROOT)

SENT = []
ap.telegram = lambda text, *a, **k: SENT.append(text) or True

# per scenario: sym -> (trigger "HH:MM", exit "HH:MM", exit px, reason). entry 100, stop 99, target 102.
SCEN = {}


def now_hm(clock):
    return ap.ist(clock.utc()).strftime("%H:%M")


def fake_tool(clock, script, *args):
    if script.endswith("scan.py"):
        hits = [{"symbol": s} for s, v in SCEN.items() if v[0] <= now_hm(clock)]
        return 0, json.dumps({"hits": hits})
    raise AssertionError("unexpected tool call %s" % script)


def fake_evaluate(clock, sym, cfg, work):
    trig = dt.datetime.strptime(SCEN[sym][0], "%H:%M").strftime("%I:%M %p")
    plan = {"entry": 100.0, "stop": 99.0, "target": 102.0, "entry_time": trig, "risk_per_share": 1.0}
    return {"symbol": sym, "plan_path": "", "plan": plan, "edge": 9.0, "p_target": .5, "p_stop": .41,
            "hist_avg_r": .2, "hist_n": 12}, None


def fake_open(clock, c, cfg, size_mod):
    pl = c["plan"]
    return {"symbol": c["symbol"], "qty": 500, "entry": pl["entry"], "stop": pl["stop"], "target": pl["target"],
            "entry_time": pl["entry_time"], "risk": 1.0, "edge": c["edge"], "hist_avg_r": c["hist_avg_r"],
            "flat_by": cfg["flat_by"]}, None


def fake_manage(clock, pos, day, size_mod):
    _, ex_hm, px, why = SCEN[pos["symbol"]]
    if now_hm(clock) >= ex_hm:
        return px, dt.datetime.combine(day, dt.datetime.strptime(ex_hm, "%H:%M").time()), why
    return None


ap.tool, ap.evaluate, ap.open_trade, ap.manage = fake_tool, fake_evaluate, fake_open, fake_manage

SCENARIOS = [
    ("2026-10-13", "3 winners, B fires while A is open (must be skipped as stale), cap of 3 reached",
     {"AAA.NS": ("09:40", "10:30", 102.0, "target"), "BBB.NS": ("10:00", "10:30", 102.0, "target"),
      "CCC.NS": ("10:40", "11:30", 102.0, "target"), "DDD.NS": ("11:35", "12:00", 102.0, "target"),
      "EEE.NS": ("12:10", "12:40", 102.0, "target")}),
    ("2026-10-14", "2 losers then loss cut-off: day net <= -1000 must stop new entries",
     {"AAA.NS": ("09:40", "10:00", 99.0, "stop"), "CCC.NS": ("10:05", "10:30", 99.0, "stop"),
      "DDD.NS": ("10:40", "11:00", 102.0, "target")}),
    ("2026-10-15", "no breakouts at all", {}),
    ("2026-10-16", "1 loser then 1 winner, then nothing more before 14:45",
     {"AAA.NS": ("09:40", "10:00", 99.0, "stop"), "CCC.NS": ("13:00", "15:10", 101.0, "flat by 15:10")}),
]

for date, title, scen in SCENARIOS:
    SCEN.clear()
    SCEN.update(scen)
    SENT.clear()
    print("=" * 80)
    print("SCENARIO %s: %s" % (date, title))
    clock = ap.Clock(dt.date.fromisoformat(date), "offline-stub")
    ap.run_day(clock, ap.config())
    st = json.load(open(os.path.join(ap.STATE_DIR, "autopilot-%s.json" % date)))
    print("-- state: status=%s trades=%s stop_reason=%r" % (st["status"], st["trades"], st.get("stop_reason")))
    print("-- closed: %s" % [(p["symbol"], p["entry_time"], p["exit_time"], p["net"]) for p in st["closed"]])
    print("-- skips: %s" % st["skips"])
    print("-- telegram:")
    for m in SENT:
        print("   | " + m)

print("=" * 80)
print("JOURNAL:")
print(open(os.path.join(ap.TRADER, "journal.csv")).read())
