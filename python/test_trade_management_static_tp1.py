import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bot_engine import TradingEngine


class DummyMT5:
    def estimate_profit_usd(self, symbol, side, volume, open_price, close_price, symbol_info=None):
        direction = str(side or "").upper()
        delta = (float(close_price) - float(open_price)) if direction == "BUY" else (float(open_price) - float(close_price))
        return round(delta * float(volume) * 100.0, 6)

    def _resolve_partial_volume(self, symbol, requested_volume, position_volume):
        req = max(0.0, float(requested_volume or 0.0))
        pos = max(0.0, float(position_volume or 0.0))
        step = 0.01
        min_lot = 0.01
        if req <= 0.0:
            return {"ok": False, "reason": "REQUESTED_VOLUME_NON_POSITIVE"}
        close_volume = round(int(min(req, pos) / step) * step, 2)
        if close_volume < min_lot:
            return {"ok": False, "reason": "VOLUME_BELOW_MIN_LOT"}
        remaining = round(max(0.0, pos - close_volume), 2)
        if remaining > 0.0 and remaining < min_lot:
            return {"ok": False, "reason": "REMAINDER_BELOW_MIN_LOT"}
        if close_volume >= pos:
            return {"ok": False, "reason": "PARTIAL_EQUALS_FULL_POSITION"}
        return {"ok": True, "volume": close_volume, "remaining": remaining}


class StaticTp1PlanningTests(unittest.TestCase):
    def setUp(self):
        self.engine = TradingEngine.__new__(TradingEngine)
        self.engine.mt5 = DummyMT5()
        self.partials_cfg = {
            "use_static_tp1_usd": True,
            "tp1_r": 1.0,
            "tp1_static_usd": 55.0,
            "tp1_static_usd_min": 50.0,
            "tp1_static_usd_max": 60.0,
            "min_tp2_remaining_usd": 25.0,
            "fallback_to_single_tp_if_small_trade": True,
        }

    def test_static_tp1_buy_plan_targets_fixed_cash_and_leaves_runner(self):
        plan = self.engine._build_static_tp1_plan(
            position={"symbol": "EURUSD", "type": "BUY", "open_price": 100.0, "volume": 1.0, "tp": 102.0},
            open_trade={"tp_price": 102.0},
            partials_cfg=self.partials_cfg,
            initial_risk=1.0,
            original_volume=1.0,
            symbol_info={},
        )

        self.assertTrue(plan["enabled"])
        self.assertFalse(plan["skip_partials"])
        self.assertAlmostEqual(plan["full_tp_usd"], 200.0, places=2)
        self.assertAlmostEqual(plan["tp1_full_position_usd"], 100.0, places=2)
        self.assertAlmostEqual(plan["close_volume"], 0.55, places=2)
        self.assertAlmostEqual(plan["tp1_target_usd"], 55.0, places=2)
        self.assertAlmostEqual(plan["remaining_tp2_usd"], 90.0, places=2)

    def test_static_tp1_sell_plan_handles_direction_correctly(self):
        plan = self.engine._build_static_tp1_plan(
            position={"symbol": "EURUSD", "type": "SELL", "open_price": 100.0, "volume": 1.0, "tp": 98.0},
            open_trade={"tp_price": 98.0},
            partials_cfg=self.partials_cfg,
            initial_risk=1.0,
            original_volume=1.0,
            symbol_info={},
        )

        self.assertTrue(plan["enabled"])
        self.assertFalse(plan["skip_partials"])
        self.assertAlmostEqual(plan["close_volume"], 0.55, places=2)
        self.assertAlmostEqual(plan["remaining_tp2_usd"], 90.0, places=2)

    def test_static_tp1_small_trade_falls_back_to_single_tp(self):
        plan = self.engine._build_static_tp1_plan(
            position={"symbol": "EURUSD", "type": "BUY", "open_price": 100.0, "volume": 0.4, "tp": 102.0},
            open_trade={"tp_price": 102.0},
            partials_cfg=self.partials_cfg,
            initial_risk=1.0,
            original_volume=0.4,
            symbol_info={},
        )

        self.assertTrue(plan["enabled"])
        self.assertTrue(plan["skip_partials"])
        self.assertEqual(plan["reason"], "STATIC_TP1_SMALL_TRADE_SINGLE_TP")
        self.assertAlmostEqual(plan["full_tp_usd"], 80.0, places=2)
        self.assertAlmostEqual(plan["remaining_tp2_usd"], 80.0, places=2)


if __name__ == "__main__":
    unittest.main()
