"""market: IN (NSE). Runs the one-trade chain on a SYNTHETIC NSE chart
(tests/make_in_fixture.py: TSLA's real bars moved onto the 9:15-15:30 IST clock, x4)."""

import json
import os
import subprocess
import sys
import tempfile
import unittest

from _load import ROOT, FIX

CHART = os.path.join(FIX, "market-in", "DEMO.NS-60d5m.json.gz")
NIFTY_CSV = ("Company Name,Industry,Symbol,Series,ISIN Code\n"
             "Reliance Industries Ltd.,Oil Gas & Consumable Fuels,RELIANCE,EQ,INE002A01018\n"
             "Mahindra & Mahindra Ltd.,Automobile and Auto Components,M&M,EQ,INE101A01026\n"
             "Bajaj Auto Ltd.,Automobile and Auto Components,BAJAJ-AUTO,EQ,INE917I01010\n")
CONFIG = {"market": "IN", "budget": 100000, "max_loss_per_trade_usd": 500, "max_position_pct": 50,
          "cash_buffer_pct": 10, "paper": True}


class MarketIN(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = cls.tmp.name
        os.makedirs(os.path.join(cls.dir, "trader"))
        with open(os.path.join(cls.dir, "trader", "config.json"), "w") as fh:
            json.dump(CONFIG, fh)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_tool(self, script, *args):
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        env.pop("TRADER_MARKET", None)   # the market comes from trader/config.json
        p = subprocess.run([sys.executable, "-I", os.path.join(ROOT, script)] + list(args), cwd=self.dir,
                           env=env, capture_output=True, text=True, encoding="utf-8", timeout=120)
        return p.returncode, p.stdout + p.stderr

    def read(self, name):
        with open(os.path.join(self.dir, "trader", name), encoding="utf-8") as fh:
            return json.load(fh)

    def test_1_chain(self):
        now = ["--now", "2026-10-19 10:00"]
        code, out = self.run_tool("trader-entry/entry.py", "check", "DEMO", "--offline", CHART, "--write", *now)
        self.assertEqual(code, 0, out)
        self.assertIn("Opening range 9:15-9:30", out)
        self.assertIn("TRIGGERED 10:00 AM at ₹1456.70", out)
        self.assertEqual(self.read("entry.json")["symbol"], "DEMO.NS")

        code, out = self.run_tool("trader-exit/exit.py", "plan", "--target", "1R", "--write", *now)
        self.assertEqual(code, 0, out)
        plan = self.read("plan.json")
        self.assertEqual((plan["stop"], plan["target"], plan["flat_by"]), (1433.3, 1480.1, "15:10"))
        self.assertIn("15:10 IST", out)

        code, out = self.run_tool("trader-timemachine/timemachine.py", "run", "--offline", CHART, "--write", *now)
        self.assertEqual(code, 0, out)
        self.assertIn("10:00 AM to 3:30 PM (66 five-minute bars)", out)

        code, out = self.run_tool("trader-budget/size.py", "--write", *now)
        self.assertEqual(code, 0, out)
        order = self.read("orders.json")["orders"][0]
        self.assertEqual(order["qty"], "20")        # 21 x 23.40 = 491.40 fits 500 alone, not with charges
        self.assertIn("Charges if the stop fills", out)

        code, out = self.run_tool("trader-risk/check.py", "--now", "2026-10-19 10:02")
        self.assertEqual(code, 0, out)
        self.assertIn("1 passed, 0 blocked", out)

        code, out = self.run_tool("trader-broker/ticket.py", "show", "--now", "2026-10-19 10:03")
        self.assertIn("no NSE broker connector", out)
        self.assertNotIn("place_stock_order", out)

    def test_2_nse_holiday_blocks_entries(self):
        code, out = self.run_tool("trader-alerts/alerts.py", "clock", "--now", "2026-11-24 10:00")
        self.assertIn("NSE holiday: Prakash Gurpurb", out)
        self.assertIn("Next open: 2026-11-25 09:15 IST", out)

    def test_3_universe_from_nifty_csv(self):
        path = os.path.join(self.dir, "ind_nifty100list.csv")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(NIFTY_CSV)
        code, out = self.run_tool("trader-scanner/scan.py", "universe", "--offline", path)
        self.assertEqual(code, 0, out)
        with open(os.path.join(self.dir, "trader", "universe.txt"), encoding="utf-8") as fh:
            syms = [ln.split("|")[0] for ln in fh.read().splitlines()]
        self.assertEqual(syms, ["RELIANCE.NS", "M&M.NS", "BAJAJ-AUTO.NS"])


class Charges(unittest.TestCase):
    def test_round_trip(self):
        os.environ["TRADER_MARKET"] = "IN"
        try:
            from _load import load
            size = load("trader-budget", "size")
        finally:
            del os.environ["TRADER_MARKET"]
            sys.stdout, sys.stderr = sys.__stdout__, sys.__stderr__
        # buy 20 x 1456.70, sell 20 x 1433.30: brokerage 0.03% (under the Rs 20 cap) 8.74 + 8.60,
        # exchange 1.77, SEBI 0.06, GST 3.45, STT 7 (rounded), stamp 0.87
        got = size.round_trip_charges(20, 1456.70, 1433.30)
        self.assertAlmostEqual(got, 17.34 + 1.77 + 0.06 + 3.45 + 7 + 0.87, delta=0.05)


if __name__ == "__main__":
    unittest.main()
