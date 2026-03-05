import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from trading_memory import TradeMemory, TradingMemoryDB


class TestTradeMemorySync(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(prefix="tm_sync_", suffix=".db")
        os.close(fd)
        self.db_path = Path(path)
        self.db = TradingMemoryDB(self.db_path)

    def tearDown(self):
        try:
            self.db.close()
        finally:
            if self.db_path.exists():
                self.db_path.unlink()

    def _pending_trade(self, ticket: int) -> TradeMemory:
        return TradeMemory(
            ticket=ticket,
            order_ticket=ticket,
            deal_ticket=None,
            position_id=None,
            symbol="EURUSD",
            direction="BUY",
            setup_type="FVG",
            entry_price=1.1000,
            sl_price=1.0950,
            tp_price=1.1100,
            lot_size=0.10,
            htf_bias="UNKNOWN",
            kill_zone="UNKNOWN",
            spread_pips=1.2,
            reason="pending",
            conditions_met=["PENDING"],
            expected_outcome="",
            confidence_input=0.5,
            setup_class="UNKNOWN",
            validity_tags=["PENDING"],
            entry_time=datetime(2026, 3, 4, 10, 0, 0),
        )

    def test_get_open_trades_excludes_pending_by_default(self):
        self.db.record_entry(self._pending_trade(111001))
        open_default = self.db.get_open_trades()
        open_with_pending = self.db.get_open_trades(include_pending=True)
        self.assertEqual(len(open_default), 0)
        self.assertEqual(len(open_with_pending), 1)

    def test_ensure_open_trade_from_position_links_pending_entry(self):
        ticket = 111002
        self.db.record_entry(self._pending_trade(ticket))
        action = self.db.ensure_open_trade_from_position(
            {
                "ticket": ticket,
                "symbol": "EURUSD",
                "type": "BUY",
                "volume": 0.10,
                "open_price": 1.1002,
                "sl": 1.0950,
                "tp": 1.1100,
                "comment": "ICT_FVG_LIMIT",
                "open_time": datetime(2026, 3, 4, 10, 5, 0),
            }
        )
        self.assertEqual(action, "linked")
        open_now = self.db.get_open_trades()
        self.assertEqual(len(open_now), 1)
        self.assertEqual(int(open_now[0]["position_id"]), ticket)

    def test_entry_deal_sync_then_exit_records_closed_trade(self):
        position_id = 222333
        action = self.db.ensure_entry_trade_from_deal(
            {
                "entry": 0,
                "position_id": position_id,
                "order_ticket": position_id,
                "deal_ticket": 999001,
                "symbol": "USDCHF",
                "type": "BUY",
                "price": 0.78131,
                "volume": 0.17,
                "time": "2026-03-04T10:31:14",
                "magic": 20250101,
                "comment": "ICT_SNIPER",
            }
        )
        self.assertEqual(action, "inserted")
        self.assertEqual(len(self.db.get_open_trades()), 1)

        updated = self.db.record_exit(
            position_id=position_id,
            order_ticket=position_id,
            deal_ticket=999002,
            exit_price=0.78034,
            pnl=-16.49,
            exit_time=datetime(2026, 3, 4, 11, 0, 0),
        )
        self.assertTrue(updated)
        self.assertEqual(len(self.db.get_open_trades()), 0)

        last = self.db.get_last_trades_raw(1)[0]
        self.assertEqual(last["outcome"], "LOSS")
        self.assertAlmostEqual(float(last["pnl"]), -16.49, places=6)

    def test_trade_management_state_persists_across_restart(self):
        trade_id = "555001"
        self.assertIsNone(self.db.get_trade_mgmt_state(trade_id))

        saved = self.db.upsert_trade_mgmt_state(
            trade_id=trade_id,
            tp1_done=True,
            tp2_done=False,
            initial_risk=0.0042,
            original_volume=0.30,
            peak_r=1.74,
            activated_giveback=True,
            opened_ts="2026-03-05T08:30:00",
        )
        self.assertIsNotNone(saved)
        self.assertTrue(saved["tp1_done"])
        self.assertFalse(saved["tp2_done"])
        self.assertAlmostEqual(float(saved["initial_risk"]), 0.0042, places=8)
        self.assertAlmostEqual(float(saved["original_volume"]), 0.30, places=8)
        self.assertAlmostEqual(float(saved["peak_r"]), 1.74, places=8)
        self.assertTrue(saved["activated_giveback"])

        self.db.close()
        reopened = TradingMemoryDB(self.db_path)
        try:
            loaded = reopened.get_trade_mgmt_state(trade_id)
            self.assertIsNotNone(loaded)
            self.assertTrue(loaded["tp1_done"])
            self.assertFalse(loaded["tp2_done"])
            self.assertAlmostEqual(float(loaded["initial_risk"]), 0.0042, places=8)
            self.assertAlmostEqual(float(loaded["original_volume"]), 0.30, places=8)
            self.assertAlmostEqual(float(loaded["peak_r"]), 1.74, places=8)
            self.assertTrue(loaded["activated_giveback"])
        finally:
            reopened.close()


if __name__ == "__main__":
    unittest.main()

