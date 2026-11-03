import os
import tempfile
import threading
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta

import astratrade_gui as app


def make_bars(count=260, start=100.0, step=0.25):
    rows = []
    origin = datetime(2025, 1, 2)
    price = start
    for i in range(count):
        price += step
        rows.append({
            "t": (origin + timedelta(days=i)).isoformat() + "Z",
            "o": price - 0.10,
            "h": price + 0.50,
            "l": price - 0.50,
            "c": price,
            "v": 100000,
        })
    return rows


class FakeBroker:
    def __init__(self, orders):
        self.orders = list(orders)
        self.position_rows = [{"symbol": "AAPL", "qty": "10", "market_value": "1000"}]
        self.cancelled = []
        self.closed = []

    def positions(self):
        return list(self.position_rows)

    def open_orders(self):
        return list(self.orders)

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        self.orders = [o for o in self.orders if o.get("id") != order_id]
        return {}

    def close_position(self, symbol):
        self.closed.append(symbol)
        self.position_rows = [p for p in self.position_rows if p.get("symbol") != symbol]
        return {"status": "new"}


class AstraTradeCoreTests(unittest.TestCase):
    def test_analyze_requires_at_least_220_completed_daily_bars(self):
        self.assertIsNone(app.analyze(make_bars(219)))
        result = app.analyze(make_bars(220))
        self.assertIsNotNone(result)
        self.assertIn("conditions", result)
        self.assertEqual(len(result["conditions"]), 6)
        self.assertGreaterEqual(result["score"], 0.0)
        self.assertLessEqual(result["score"], 1.0)

    def test_prepare_daily_signal_rows_excludes_incomplete_session(self):
        rows = make_bars(250)
        rows.append({
            "t": "2026-11-03T14:30:00Z",
            "o": 160.0, "h": 161.0, "l": 159.0, "c": 160.5, "v": 5000
        })
        completed, current_price, fresh = app.prepare_daily_signal_rows(rows, True, "2026-11-03")
        self.assertEqual(len(completed), 250)
        self.assertEqual(current_price, 160.5)
        self.assertTrue(fresh)
        self.assertEqual(completed[-1]["t"][:10], rows[-2]["t"][:10])

    def test_prepare_daily_signal_rows_blocks_stale_live_price(self):
        rows = make_bars(250)
        completed, current_price, fresh = app.prepare_daily_signal_rows(rows, True, "2026-11-03")
        self.assertEqual(len(completed), 250)
        self.assertIsNone(current_price)
        self.assertFalse(fresh)

    def test_prepare_daily_signal_rows_uses_last_close_when_market_closed(self):
        rows = make_bars(250)
        completed, current_price, fresh = app.prepare_daily_signal_rows(rows, False, "2026-11-03")
        self.assertEqual(len(completed), 250)
        self.assertEqual(current_price, rows[-1]["c"])
        self.assertFalse(fresh)

    def test_common_stock_filter_excludes_funds_and_non_common_assets(self):
        base = {"symbol": "ABC", "tradable": True, "status": "active", "exchange": "NASDAQ"}
        self.assertTrue(app.is_eligible_common_stock({**base, "name": "ABC Class A Common Stock"}))
        self.assertFalse(app.is_eligible_common_stock({**base, "name": "ABC ETF Common Stock"}))
        self.assertFalse(app.is_eligible_common_stock({**base, "name": "ABC Warrant"}))
        self.assertFalse(app.is_eligible_common_stock({**base, "name": "ABC Common Stock", "exchange": "OTC"}))
        self.assertFalse(app.is_eligible_common_stock({**base, "name": "ABC Common Stock", "tradable": False}))

    def test_order_sizing_enforces_risk_position_exposure_and_cash(self):
        qty = app.calculate_order_qty(100000, 50000, 100, 5, 0)
        self.assertEqual(qty, 50)  # USD 250 risk budget is binding.
        self.assertLessEqual(qty * 5, 100000 * 0.0025)
        self.assertLessEqual(qty * 100, 100000 * 0.07)

        position_limited = app.calculate_order_qty(100000, 50000, 100, 0.1, 0)
        self.assertEqual(position_limited, 70)

        exposure_limited = app.calculate_order_qty(100000, 50000, 100, 5, 29000)
        self.assertEqual(exposure_limited, 10)

        cash_limited = app.calculate_order_qty(100000, 500, 100, 5, 0)
        self.assertEqual(cash_limited, 4)

    def test_managed_exit_cancels_only_own_bracket_legs_then_closes(self):
        orders = [
            {"id": "tp", "symbol": "AAPL", "side": "sell", "group_id": "astra-group", "group_role": "take_profit", "limit_price": "120", "status": "new"},
            {"id": "sl", "symbol": "AAPL", "side": "sell", "group_id": "astra-group", "group_role": "stop_loss", "stop_price": "90", "status": "held"},
            {"id": "msft-tp", "symbol": "MSFT", "side": "sell", "group_id": "other-group", "group_role": "take_profit", "limit_price": "500", "status": "new"},
        ]
        broker = FakeBroker(orders)
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(app, "STATE_FILE", os.path.join(tmp, "state.json")), \
             patch.object(app, "load_managed_symbols", return_value={"AAPL"}), \
             patch.object(app, "load_managed_groups", return_value={"AAPL": "astra-group"}), \
             patch.object(app, "load_managed_protection", return_value={"AAPL": {"take_profit": 120, "stop_loss": 90}}):
            bot = app.Bot(broker, queue_for_test(), threading.Event())
            self.assertTrue(bot._close_managed_position("AAPL", broker.open_orders()))
        self.assertEqual(set(broker.cancelled), {"tp", "sl"})
        self.assertEqual(broker.closed, ["AAPL"])
        self.assertEqual([o["id"] for o in broker.orders], ["msft-tp"])

    def test_managed_exit_does_not_cancel_manual_sell_order_on_same_symbol(self):
        orders = [
            {"id": "tp", "symbol": "AAPL", "side": "sell", "group_id": "astra-group", "group_role": "take_profit", "limit_price": "120", "status": "new"},
            {"id": "sl", "symbol": "AAPL", "side": "sell", "group_id": "astra-group", "group_role": "stop_loss", "stop_price": "90", "status": "held"},
            {"id": "manual", "symbol": "AAPL", "side": "sell", "type": "limit", "limit_price": "130", "status": "new"},
        ]
        broker = FakeBroker(orders)
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(app, "STATE_FILE", os.path.join(tmp, "state.json")), \
             patch.object(app, "load_managed_symbols", return_value={"AAPL"}), \
             patch.object(app, "load_managed_groups", return_value={"AAPL": "astra-group"}), \
             patch.object(app, "load_managed_protection", return_value={"AAPL": {"take_profit": 120, "stop_loss": 90}}):
            bot = app.Bot(broker, queue_for_test(), threading.Event())
            self.assertFalse(bot._close_managed_position("AAPL", broker.open_orders()))
        self.assertEqual(broker.cancelled, [])
        self.assertEqual(broker.closed, [])

def queue_for_test():
    import queue
    return queue.Queue()


if __name__ == "__main__":
    unittest.main()
