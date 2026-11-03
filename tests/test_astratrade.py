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
        if getattr(self, "close_error", None):
            raise RuntimeError(self.close_error)
        self.position_rows = [p for p in self.position_rows if p.get("symbol") != symbol]
        return {"status": "new"}

    def restore_oco_protection(self, symbol, qty, take_profit, stop_loss):
        if not hasattr(self, "restored"):
            self.restored = []
        self.restored.append((symbol, qty, take_profit, stop_loss))
        return {"group_id": "restored-group", "status": "new"}


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

    def test_live_entry_confirmation_rejects_failed_breakout_and_chasing(self):
        signal = {"price": 100.0, "ema20": 98.0, "prior_20_high": 99.0, "atr": 4.0}
        self.assertTrue(app.confirm_live_entry(signal, 101.0))
        self.assertFalse(app.confirm_live_entry(signal, 98.0))
        self.assertFalse(app.confirm_live_entry(signal, 107.0))

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


    def test_exit_failure_restores_original_oco_protection(self):
        orders = [
            {"id": "tp", "symbol": "AAPL", "side": "sell", "group_id": "astra-group", "group_role": "take_profit", "limit_price": "120", "status": "new"},
            {"id": "sl", "symbol": "AAPL", "side": "sell", "group_id": "astra-group", "group_role": "stop_loss", "stop_price": "90", "status": "held"},
        ]
        broker = FakeBroker(orders)
        broker.close_error = "simulated close rejection"
        stop = threading.Event()
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(app, "STATE_FILE", os.path.join(tmp, "state.json")), \
             patch.object(app, "load_managed_symbols", return_value={"AAPL"}), \
             patch.object(app, "load_managed_groups", return_value={"AAPL": "astra-group"}), \
             patch.object(app, "load_managed_protection", return_value={"AAPL": {"take_profit": 120, "stop_loss": 90}}):
            bot = app.Bot(broker, queue_for_test(), stop)
            self.assertFalse(bot._close_managed_position("AAPL", broker.open_orders()))
            self.assertFalse(stop.is_set())
            self.assertEqual(bot.managed_groups["AAPL"], "restored-group")
        self.assertEqual(broker.restored, [("AAPL", 10.0, 120.0, 90.0)])

    def test_one_scan_sends_a_sized_bracket_order_when_all_gates_pass(self):
        class OneCycleStop:
            def __init__(self):
                self.stopped = False
            def is_set(self):
                return self.stopped
            def wait(self, seconds):
                self.stopped = True
            def set(self):
                self.stopped = True

        class ScanBroker:
            def __init__(self):
                self.sent = []
                self.rows = make_bars(250)
                self.rows.append({
                    "t": "2026-11-03T16:00:00Z",
                    "o": 101.0, "h": 103.0, "l": 100.0, "c": 102.0, "v": 120000
                })
            def account(self):
                return {"equity": "100000", "cash": "100000", "last_equity": "100000"}
            def assets(self):
                return [{"symbol": "AAPL", "name": "Apple Inc. Common Stock", "exchange": "NASDAQ", "status": "active", "tradable": True}]
            def bars(self, symbols):
                return {"bars": {symbol: self.rows for symbol in symbols}}
            def clock(self):
                return {"is_open": True, "timestamp": "2026-11-03T16:00:00Z"}
            def positions(self):
                return []
            def open_orders(self):
                return []
            def order(self, symbol, qty, side, take_profit=None, stop_loss=None):
                self.sent.append((symbol, qty, side, take_profit, stop_loss))
                return {"status": "accepted", "group_id": "scan-group"}

        signal = {
            "price": 100.0, "ema20": 99.0, "ema50": 95.0, "sma200": 90.0,
            "macd_hist": 1.0, "rsi": 60.0, "atr": 2.0,
            "dollar_vol": 20_000_000.0, "relative_volume": 1.2,
            "prior_20_high": 99.5, "score": 5/6, "action": "BUY",
            "conditions": {
                "uzun_trend": True, "trend_sirasi": True, "macd_pozitif": True,
                "rsi_uygun": True, "20_gun_tepe": True, "hacim_onayi": False
            }
        }
        broker = ScanBroker()
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(app, "STATE_FILE", os.path.join(tmp, "state.json")), \
             patch.object(app, "load_managed_symbols", return_value=set()), \
             patch.object(app, "load_managed_groups", return_value={}), \
             patch.object(app, "load_managed_protection", return_value={}), \
             patch.object(app, "analyze", return_value=signal):
            bot = app.Bot(broker, queue_for_test(), OneCycleStop())
            bot.run()
        self.assertEqual(len(broker.sent), 1)
        symbol, qty, side, tp, sl = broker.sent[0]
        self.assertEqual(symbol, "AAPL")
        self.assertEqual(qty, 62)
        self.assertEqual(side, "buy")
        self.assertGreater(tp, sl)
        self.assertEqual(bot.managed_symbols, {"AAPL"})

    def test_one_scan_does_not_send_buy_when_market_is_closed(self):
        class OneCycleStop:
            def __init__(self):
                self.stopped = False
            def is_set(self):
                return self.stopped
            def wait(self, seconds):
                self.stopped = True
            def set(self):
                self.stopped = True

        class ScanBroker:
            def __init__(self):
                self.sent = []
                self.rows = make_bars(250)
            def account(self):
                return {"equity": "100000", "cash": "100000", "last_equity": "100000"}
            def assets(self):
                return [{"symbol": "AAPL", "name": "Apple Inc. Common Stock", "exchange": "NASDAQ", "status": "active", "tradable": True}]
            def bars(self, symbols):
                return {"bars": {symbol: self.rows for symbol in symbols}}
            def clock(self):
                return {"is_open": False, "timestamp": "2026-11-03T16:00:00Z"}
            def positions(self):
                return []
            def open_orders(self):
                return []
            def order(self, *args, **kwargs):
                self.sent.append((args, kwargs))
                return {"status": "accepted"}

        signal = {
            "price": 100.0, "ema20": 99.0, "ema50": 95.0, "sma200": 90.0,
            "macd_hist": 1.0, "rsi": 60.0, "atr": 2.0,
            "dollar_vol": 20_000_000.0, "relative_volume": 1.2,
            "prior_20_high": 99.5, "score": 5/6, "action": "BUY",
            "conditions": {
                "uzun_trend": True, "trend_sirasi": True, "macd_pozitif": True,
                "rsi_uygun": True, "20_gun_tepe": True, "hacim_onayi": False
            }
        }
        broker = ScanBroker()
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(app, "STATE_FILE", os.path.join(tmp, "state.json")), \
             patch.object(app, "load_managed_symbols", return_value=set()), \
             patch.object(app, "load_managed_groups", return_value={}), \
             patch.object(app, "load_managed_protection", return_value={}), \
             patch.object(app, "analyze", return_value=signal):
            bot = app.Bot(broker, queue_for_test(), OneCycleStop())
            bot.run()
        self.assertEqual(broker.sent, [])

    def test_open_orders_paginates_past_500_and_deduplicates_ids(self):
        broker = app.Alpaca("paper-key", "paper-secret")
        first_page = [{"id": f"order-{i}"} for i in range(500)]
        second_page = [{"id": "order-500"}]
        with patch.object(broker, "call", side_effect=[first_page, second_page]) as mocked:
            orders = broker.open_orders()
        self.assertEqual(len(orders), 501)
        self.assertEqual(mocked.call_count, 2)
        second_params = mocked.call_args_list[1].args[2]
        self.assertEqual(second_params["after_order_id"], "order-499")
        self.assertEqual(second_params["nested"], "false")
    def test_order_requests_are_bracket_and_recovery_oco(self):
        broker = app.Alpaca("paper-key", "paper-secret")
        with patch.object(broker, "call", return_value={"status": "new"}) as mocked:
            broker.order("AAPL", 5, "buy", take_profit=120.0, stop_loss=90.0)
            body = mocked.call_args.kwargs["body"]
            self.assertEqual(body["order_class"], "bracket")
            self.assertEqual(body["time_in_force"], "gtc")
            self.assertEqual(body["take_profit"]["limit_price"], "120.00")
            self.assertEqual(body["stop_loss"]["stop_price"], "90.00")
        with patch.object(broker, "call", return_value={"status": "new"}) as mocked:
            broker.restore_oco_protection("AAPL", 5, 120.0, 90.0)
            body = mocked.call_args.kwargs["body"]
            self.assertEqual(body["side"], "sell")
            self.assertEqual(body["type"], "limit")
            self.assertEqual(body["order_class"], "oco")
            self.assertEqual(body["time_in_force"], "gtc")

    def test_backtest_checks_stop_on_the_entry_day(self):
        rows = []
        origin = datetime(2025, 1, 2)
        for i in range(250):
            rows.append({
                "t": (origin + timedelta(days=i)).isoformat() + "Z",
                "o": 100.0, "h": 110.0, "l": 80.0, "c": 100.0, "v": 100000
            })
        signal = {
            "price": 100.0, "sma200": 90.0, "ema50": 95.0,
            "atr": 5.0, "ema20": 95.0, "prior_20_high": 99.0, "action": "BUY"
        }
        with patch.object(app, "analyze", return_value=signal):
            result = app.backtest_strategy(rows, rows)
        self.assertGreater(result["trades"], 0)
        self.assertEqual(result["trades_detail"][0]["reason"], "GİRİŞ GÜNÜ STOP")

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI is available only on Windows")
    def test_credentials_round_trip_uses_windows_dpapi(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(app, "CREDENTIAL_FILE", os.path.join(tmp, "credentials.dat")):
            app.save_credentials("TEST-PAPER-KEY", "TEST-PAPER-SECRET")
            self.assertEqual(app.load_credentials(), ("TEST-PAPER-KEY", "TEST-PAPER-SECRET"))

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
