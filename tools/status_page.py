#!/usr/bin/env python3
"""Render the paper autopilot's status as one self-contained HTML page.

    python3 -I status_page.py --out /path/to/page.html [--home /path/to/day-trader-skill]

--home is the folder that holds trader/ (default: this script's own folder). Read-only on trader/: it reads journal.csv, autopilot.log, today's state file, config.json and
autopilot.json (never telegram.json), and writes only the --out file (atomically). Standard library only.
"""

import argparse
import csv
import datetime as dt
import html
import json
import os
import tempfile

HOME = os.path.dirname(os.path.abspath(__file__))
TRADER = os.path.join(HOME, "trader")
IST = dt.timedelta(hours=5, minutes=30)
E = html.escape


def now_ist():
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + IST


def read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def num(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def inr(x, signed=False):
    """Rupees with Indian digit grouping: 1,23,456."""
    if x is None:
        return "–"
    s = str(int(round(abs(x))))
    if len(s) > 3:
        head, parts = s[:-3], [s[-3:]]
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts)
    sign = "−" if x < 0 and round(abs(x)) else ("+" if signed and round(x) > 0 else "")
    return "%s₹%s" % (sign, s)


def px(x):
    return "–" if x is None else format(x, ",.2f")


def rr(x):
    return "–" if x is None else "%+.2fR" % x


def tone(x):
    if x is None or abs(x) < 0.5:
        return "flat"
    return "pos" if x > 0 else "neg"


def load_journal():
    rows = []
    try:
        with open(os.path.join(TRADER, "journal.csv"), newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
    except OSError:
        pass
    trades = [r for r in rows if r.get("status") == "trade"]
    for t in trades:
        for k in ("entry", "stop", "target", "exit", "net", "gross", "charges", "r", "qty", "edge_pts"):
            t[k] = num(t.get(k))
    trades.sort(key=lambda t: (t.get("date") or "", t.get("exit_time") or ""))
    others = [r for r in rows if r.get("status") != "trade"]
    return trades, others


def load_log(n=50):
    try:
        with open(os.path.join(TRADER, "autopilot.log"), encoding="utf-8", errors="replace") as fh:
            lines = [l.rstrip("\n") for l in fh if l.strip()]
    except OSError:
        lines = []
    return lines[-n:], lines


def reason_badge(reason):
    r = (reason or "").lower()
    if "target" in r:
        cls = "win"
    elif "stop" in r:
        cls = "loss"
    else:
        cls = "neutral"
    return '<span class="badge %s">%s</span>' % (cls, E(reason or "–"))


def equity_svg(trades):
    if len(trades) < 2:
        return '<p class="muted small">The equity curve appears after two paper trades.</p>'
    cum, pts = 0.0, [0.0]
    for t in trades:
        cum += t["net"] or 0
        pts.append(cum)
    w, h, pad = 600.0, 150.0, 10.0
    lo, hi = min(pts + [0.0]), max(pts + [0.0])
    span = (hi - lo) or 1.0
    x = lambda i: pad + i * (w - 2 * pad) / (len(pts) - 1)
    y = lambda v: pad + (hi - v) * (h - 2 * pad) / span
    line = " ".join("%.1f,%.1f" % (x(i), y(v)) for i, v in enumerate(pts))
    area = "%.1f,%.1f %s %.1f,%.1f" % (x(0), y(0), line, x(len(pts) - 1), y(0))
    cls = "pos" if pts[-1] >= 0 else "neg"
    dots = "".join('<circle cx="%.1f" cy="%.1f" r="3"><title>%s %s: %s (total %s)</title></circle>'
                   % (x(i + 1), y(v), E(t["date"]), E(t["symbol"]), inr(t["net"], True), inr(v, True))
                   for i, (t, v) in enumerate(zip(trades, pts[1:])))
    return ('<svg class="equity %s" viewBox="0 0 %d %d" preserveAspectRatio="none" role="img" '
            'aria-label="Cumulative paper P&amp;L, now %s">'
            '<line class="zero" x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f"/>'
            '<polygon class="area" points="%s"/><polyline class="line" points="%s"/>'
            '<g class="dots">%s</g></svg>'
            '<div class="axis small muted"><span>Start</span><span>%d trades · %s</span></div>'
            % (cls, w, h, inr(pts[-1], True), pad, y(0), w - pad, y(0), area, line, dots,
               len(trades), inr(pts[-1], True)))


def today_block(now, cfg, apcfg):
    day = now.date().isoformat()
    st = read_json(os.path.join(TRADER, "state", "autopilot-%s.json" % day))
    all_lines = load_log()[1]
    today_lines = [l for l in all_lines if l.startswith(day)]
    hm = now.strftime("%H:%M")
    cap = int(apcfg.get("trades_per_day") or 1)

    if st:
        s = st.get("status")
        if s == "open":
            pill, ptone = "In a paper trade", "live"
        elif s == "watching":
            pill, ptone = "Watching for breakouts", "live"
        else:
            pill, ptone = "Done for today", "done"
    elif any("not a trading day" in l for l in today_lines):
        why = next(l for l in today_lines if "not a trading day" in l).split("not a trading day", 1)[1]
        pill, ptone = "Market holiday" + (" " + why.strip() if why.strip() else ""), "idle"
    elif now.weekday() >= 5:
        pill, ptone = "Weekend: market closed", "idle"
    elif hm < "09:25":
        pill, ptone = "Starts at 09:25 IST", "idle"
    elif hm < "09:40":
        pill, ptone = "Starting up", "live"
    elif any("crashed" in l.lower() for l in today_lines):
        pill, ptone = "Crashed: check the log", "warn"
    else:
        pill, ptone = "No run found today: check the log", "warn"

    closed = list((st or {}).get("closed") or [])
    pos = (st or {}).get("position") if (st or {}).get("status") == "open" else None
    day_net = sum(num(p.get("net"), 0) for p in closed)

    out = ['<section class="card today"><div class="today-head"><div>'
           '<p class="eyebrow">Today · %s</p><h2>%s</h2></div>'
           '<span class="pill %s"><span class="dot"></span>%s</span></div>'
           % (E(now.strftime("%a %d %b %Y")), inr(day_net, True) if closed else "No trades yet",
              ptone, E(pill))]
    out.append('<p class="muted small">%d of %d trades used · %d breakouts looked at</p>'
               % (len(closed) + (1 if pos else 0), cap, len((st or {}).get("seen") or [])))
    if pos:
        risk = num(pos.get("risk"), 0) * num(pos.get("qty"), 0)
        out.append('<div class="position"><div class="pos-head"><strong>%s</strong>'
                   '<span class="muted small">since %s IST · %d shares</span></div>'
                   '<div class="levels"><div><span class="lbl">Entry</span><b>%s</b></div>'
                   '<div><span class="lbl">Stop</span><b class="neg">%s</b></div>'
                   '<div><span class="lbl">Target</span><b class="pos">%s</b></div>'
                   '<div><span class="lbl">Max loss</span><b>%s</b></div></div></div>'
                   % (E(pos.get("symbol", "?")), E(pos.get("entry_time") or "?"), int(num(pos.get("qty"), 0)),
                      px(num(pos.get("entry"))), px(num(pos.get("stop"))), px(num(pos.get("target"))), inr(risk)))
    if closed:
        out.append('<ul class="today-trades">')
        for p in closed:
            n = num(p.get("net"))
            out.append('<li><span><strong>%s</strong> <span class="muted small">%s → %s</span></span>'
                       '%s<span class="amt %s">%s <small>%s</small></span></li>'
                       % (E(p.get("symbol", "?")), E(p.get("entry_time") or ""), E(p.get("exit_time") or ""),
                          reason_badge(p.get("exit_reason")), tone(n), inr(n, True), rr(num(p.get("r")))))
        out.append("</ul>")
    if st and st.get("stop_reason"):
        out.append('<p class="muted small">Stopped: %s</p>' % E(st["stop_reason"]))
    skips = (st or {}).get("skips") or []
    if skips and not pos:
        out.append('<p class="muted small">Recent skips: %s</p>' % E("; ".join(skips[-3:])))
    out.append("</section>")
    return "".join(out)


def stats_block(trades, others):
    nets = [t["net"] or 0 for t in trades]
    wins = [n for n in nets if n > 0]
    total = sum(nets)
    rs = [t["r"] for t in trades if t["r"] is not None]
    days = len({t["date"] for t in trades} | {o.get("date") for o in others})
    cards = [
        ("Net paper P&L", inr(total, True), tone(total), "after charges"),
        ("Trades", str(len(trades)), "", "%d days run" % days),
        ("Win rate", ("%.0f%%" % (100.0 * len(wins) / len(trades))) if trades else "–", "",
         "%d won · %d lost" % (len(wins), len(trades) - len(wins))),
        ("Average R", ("%+.2f" % (sum(rs) / len(rs))) if rs else "–", tone(sum(rs)) if rs else "",
         "per trade"),
        ("Best trade", inr(max(nets), True) if nets else "–", "pos" if nets and max(nets) > 0 else "",
         ""),
        ("Worst trade", inr(min(nets), True) if nets else "–", "neg" if nets and min(nets) < 0 else "",
         ""),
    ]
    return '<section class="stats">%s</section>' % "".join(
        '<div class="stat"><span class="lbl">%s</span><b class="%s">%s</b><span class="muted small">%s</span></div>'
        % (E(a), c, E(b), E(d)) for a, b, c, d in cards)


def trades_block(trades):
    if not trades:
        return ('<section class="card"><h3>Paper trades</h3><p class="muted">No paper trades yet. '
                'Days without a trade are normal: the autopilot only enters when the odds clear its bar.</p></section>')
    rows = []
    for t in reversed(trades):
        rows.append(
            '<tr><td data-l="Date">%s</td><td data-l="Stock"><strong>%s</strong></td>'
            '<td data-l="Time">%s → %s</td><td data-l="Entry" class="r">%s</td><td data-l="Exit" class="r">%s</td>'
            '<td data-l="Qty" class="r">%s</td><td data-l="Exit reason">%s</td>'
            '<td data-l="Net" class="r amt %s">%s</td><td data-l="R" class="r %s">%s</td></tr>'
            % (E(dt.date.fromisoformat(t["date"]).strftime("%d %b") if t.get("date") else "?"), E(t.get("symbol") or "?"),
               E(t.get("entry_time") or ""), E(t.get("exit_time") or ""), px(t["entry"]), px(t["exit"]),
               "%d" % t["qty"] if t["qty"] is not None else "–", reason_badge(t.get("exit_reason")),
               tone(t["net"]), inr(t["net"], True), tone(t["r"]), rr(t["r"])))
    return ('<section class="card"><h3>Paper trades <span class="muted small">newest first</span></h3>'
            '<table class="trades"><thead><tr><th>Date</th><th>Stock</th><th>Time</th><th class="r">Entry</th>'
            '<th class="r">Exit</th><th class="r">Qty</th><th>Exit reason</th><th class="r">Net</th>'
            '<th class="r">R</th></tr></thead><tbody>%s</tbody></table></section>' % "".join(rows))


def notrade_block(others):
    if not others:
        return ""
    items = "".join('<li><span class="date">%s</span><span>%s</span></li>'
                    % (E(dt.date.fromisoformat(o["date"]).strftime("%a %d %b") if o.get("date") else "?"),
                       E(o.get("note") or o.get("status") or ""))
                    for o in list(reversed(others))[:20])
    return ('<details class="card"><summary><h3>Days without a trade <span class="muted small">%d · latest 20</span></h3>'
            '</summary><ul class="notrade">%s</ul></details>' % (len(others), items))


def log_block(lines):
    body = "\n".join(E(l) for l in lines) if lines else "The log is empty: the first run writes here."
    return ('<details class="card"><summary><h3>Autopilot log <span class="muted small">last %d lines</span></h3>'
            '</summary><pre class="log">%s</pre></details>' % (len(lines), body))


CSS = """
:root{--bg:#f6f7f4;--card:#ffffff;--fg:#1d2420;--muted:#66706a;--line:#e3e7e2;--soft:#f0f3ef;
--pos:#0f7a43;--pos-bg:#e3f4ea;--neg:#c0392b;--neg-bg:#fbe9e7;--live:#0f7a43;--warn:#b26a00;--warn-bg:#fff3dc;
--accent:#0f7a43;--shadow:0 1px 2px rgba(20,30,25,.06),0 4px 16px rgba(20,30,25,.05)}
@media (prefers-color-scheme:dark){:root{--bg:#0f1311;--card:#171c19;--fg:#e7ece8;--muted:#93a097;--line:#28302b;
--soft:#1d2420;--pos:#4fd08a;--pos-bg:#173226;--neg:#ff7a6b;--neg-bg:#3a1d1a;--live:#4fd08a;--warn:#f0b357;
--warn-bg:#3a2c14;--accent:#4fd08a;--shadow:none}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,sans-serif;
font-variant-numeric:tabular-nums;-webkit-font-smoothing:antialiased}
.wrap{max-width:1040px;margin:0 auto;padding:24px 16px 48px}
header{display:flex;justify-content:space-between;align-items:flex-end;gap:12px;flex-wrap:wrap;margin-bottom:20px}
h1{font-size:22px;margin:0;letter-spacing:-.01em}h2{font-size:30px;margin:2px 0 0;letter-spacing:-.02em}
h3{font-size:15px;margin:0 0 12px;display:inline-flex;gap:8px;align-items:baseline}
.eyebrow{margin:0;font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);font-weight:600}
.muted{color:var(--muted)}.small{font-size:13px}
.tag{display:inline-block;font-size:12px;font-weight:600;padding:3px 9px;border-radius:99px;background:var(--pos-bg);color:var(--pos)}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px;box-shadow:var(--shadow);margin-bottom:16px}
.today-head{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;flex-wrap:wrap;margin-bottom:6px}
.pill{display:inline-flex;align-items:center;gap:8px;font-size:13px;font-weight:600;padding:6px 12px;border-radius:99px;background:var(--soft);color:var(--muted)}
.pill .dot{width:8px;height:8px;border-radius:50%;background:currentColor}
.pill.live{background:var(--pos-bg);color:var(--live)}.pill.live .dot{animation:pulse 1.6s infinite}
.pill.warn{background:var(--warn-bg);color:var(--warn)}
@keyframes pulse{50%{opacity:.35}}
.position{margin-top:14px;border:1px solid var(--line);border-radius:12px;padding:14px;background:var(--soft)}
.pos-head{display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap;margin-bottom:10px}
.levels{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}
.levels b{display:block;font-size:17px}
.lbl{display:block;font-size:12px;color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:.05em}
.today-trades{list-style:none;margin:14px 0 0;padding:0}
.today-trades li{display:grid;grid-template-columns:1fr auto auto;gap:12px;align-items:center;padding:10px 0;border-top:1px solid var(--line)}
.stats{display:grid;grid-template-columns:repeat(6,1fr);gap:12px;margin-bottom:16px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px;box-shadow:var(--shadow)}
.stat b{display:block;font-size:21px;margin:4px 0 2px;letter-spacing:-.01em}
.pos{color:var(--pos)}.neg{color:var(--neg)}.flat{color:var(--fg)}
.equity{width:100%;height:150px;display:block}
.equity .zero{stroke:var(--line);stroke-width:1;stroke-dasharray:4 4;vector-effect:non-scaling-stroke}
.equity .line{fill:none;stroke-width:2.5;stroke-linejoin:round;vector-effect:non-scaling-stroke}
.equity.pos .line{stroke:var(--pos)}.equity.neg .line{stroke:var(--neg)}
.equity.pos .area{fill:var(--pos-bg);opacity:.8}.equity.neg .area{fill:var(--neg-bg);opacity:.8}
.equity .dots circle{fill:var(--card);stroke:var(--muted);stroke-width:1.5;vector-effect:non-scaling-stroke}
.axis{display:flex;justify-content:space-between;margin-top:6px}
table.trades{width:100%;border-collapse:collapse;font-size:14px}
.trades th{font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);font-weight:600;text-align:left;padding:8px;border-bottom:1px solid var(--line)}
.trades td{padding:10px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
.trades tr:last-child td{border-bottom:0}
.r{text-align:right}.amt{font-weight:600}
.badge{display:inline-block;font-size:12px;font-weight:600;padding:2px 8px;border-radius:6px;background:var(--soft);color:var(--muted)}
.badge.win{background:var(--pos-bg);color:var(--pos)}.badge.loss{background:var(--neg-bg);color:var(--neg)}
details summary{cursor:pointer;list-style:none}details summary::-webkit-details-marker{display:none}
details summary h3{margin:0}details summary h3::before{content:"▸";color:var(--muted);transition:transform .15s}
details[open] summary h3::before{transform:rotate(90deg)}details[open] summary{margin-bottom:12px}
.notrade{list-style:none;margin:0;padding:0;font-size:14px}
.notrade li{display:grid;grid-template-columns:110px 1fr;gap:12px;padding:8px 0;border-top:1px solid var(--line)}
.notrade .date{color:var(--muted)}
pre.log{margin:0;max-height:420px;overflow:auto;background:var(--soft);border-radius:10px;padding:12px;font:12.5px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre-wrap;word-break:break-word}
footer{margin-top:24px;font-size:13px;color:var(--muted);display:flex;flex-direction:column;gap:4px}
@media (max-width:860px){.stats{grid-template-columns:repeat(3,1fr)}}
@media (max-width:768px){
 .wrap{padding:16px 16px 40px}h2{font-size:26px}
 .stats{grid-template-columns:repeat(2,1fr);gap:10px}.stat b{font-size:19px}
 .levels{grid-template-columns:repeat(2,1fr)}
 .today-trades li{grid-template-columns:1fr auto}.today-trades li .badge{display:none}
 table.trades thead{display:none}
 table.trades,table.trades tbody,table.trades tr,table.trades td{display:block;width:100%}
 table.trades tr{display:grid;grid-template-columns:repeat(3,1fr);gap:10px 12px;border:1px solid var(--line);border-radius:12px;padding:12px;margin-bottom:10px;background:var(--soft)}
 table.trades td,table.trades tr:last-child td{display:block;width:auto;padding:0;border:0;white-space:normal;text-align:left}
 table.trades td::before{content:attr(data-l);display:block;color:var(--muted);font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.05em;margin-bottom:1px}
 .notrade li{grid-template-columns:1fr;gap:2px}
}
"""


def render():
    now = now_ist()
    cfg = read_json(os.path.join(TRADER, "config.json")) or {}
    apcfg = read_json(os.path.join(TRADER, "autopilot.json")) or {}
    trades, others = load_journal()
    log_tail, _ = load_log(50)
    limits = "Budget %s · max loss %s a trade · %s a day · up to %s trades a day · flat by %s IST" % (
        inr(num(cfg.get("budget"))), inr(num(cfg.get("max_loss_per_trade_usd"))),
        inr(num(cfg.get("max_daily_loss_usd"))), apcfg.get("trades_per_day", 1), cfg.get("flat_by", "15:10"))
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow,noarchive">
<title>Paper Autopilot</title>
<style>%s</style></head>
<body><div class="wrap" id="app" data-gen="%s">
<header><div><p class="eyebrow">NSE · Nifty 100 · opening-range breakouts</p><h1>Paper Autopilot</h1></div>
<span class="tag">Paper trading only</span></header>
%s
%s
<section class="card"><h3>Equity curve <span class="muted small">cumulative net, all paper trades</span></h3>%s</section>
%s
%s
%s
<footer><span>%s</span><span>Updated %s IST · the server rebuilds this page every 5 minutes and it refreshes itself every 2. No orders are ever sent to a broker.</span></footer>
</div>
<script>
/* The CDN caches /static for a day, so pull a fresh copy past the cache (unique query) and swap it in. */
(function () {
  function refresh() {
    fetch(location.pathname + "?t=" + Date.now(), {cache: "no-store"})
      .then(function (r) { return r.ok ? r.text() : null; })
      .then(function (t) {
        if (!t) return;
        var n = new DOMParser().parseFromString(t, "text/html").getElementById("app");
        var o = document.getElementById("app");
        if (!n || !o || n.getAttribute("data-gen") === o.getAttribute("data-gen")) return;
        var open = Array.prototype.map.call(o.querySelectorAll("details"), function (d) { return d.open; });
        n = document.importNode(n, true);
        Array.prototype.forEach.call(n.querySelectorAll("details"), function (d, i) { if (open[i]) d.open = true; });
        o.parentNode.replaceChild(n, o);
      })
      .catch(function () {});
  }
  refresh();
  setInterval(refresh, 120000);
})();
</script>
</body></html>
""" % (CSS, E(now.isoformat(timespec="seconds")), today_block(now, cfg, apcfg), stats_block(trades, others), equity_svg(trades), trades_block(trades),
       notrade_block(others), log_block(log_tail), E(limits), E(now.strftime("%d %b %Y, %H:%M")))


def main():
    global TRADER
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True, help="the HTML file to write")
    ap.add_argument("--home", default=HOME, help="the folder that holds trader/ (default: this script's folder)")
    a = ap.parse_args()
    TRADER = os.path.join(os.path.abspath(a.home), "trader")
    page = render()
    d = os.path.dirname(os.path.abspath(a.out))
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".autopilot-", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(page)
    os.chmod(tmp, 0o644)
    os.replace(tmp, a.out)
    print("wrote %s (%d bytes)" % (a.out, len(page)))


if __name__ == "__main__":
    main()
