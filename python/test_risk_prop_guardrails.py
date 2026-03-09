import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from risk_manager import RiskManager


def _base_cfg() -> dict:
    return {
        "risk": {
            "max_daily_trades": 100,
            "max_open_trades": 10,
            "max_daily_loss_pct": 5.0,
            "max_consecutive_losses": 0,
        },
        "mode": {
            "type": "hybrid",
            "cooldown": {
                "per_symbol_seconds": 0,
                "after_loss_seconds": 1800,
                "after_win_seconds": 0,
                "global_after_loss_seconds": 0,
            },
        },
        "execution": {"profile": "normal", "prop": {"enabled": False}},
        "prop_guardrails": {
            "enabled": True,
            "daily_loss_cap_pct": 2.0,
            "close_all_on_daily_loss_breach": True,
        },
        "correlation": {
            "enabled": True,
            "max_same_thesis_open": 1,
            "max_usd_short_open": 0,
            "single_loss_risk_scale": 0.5,
            "single_loss_risk_scale_seconds": 3600,
            "cooldown_seconds_after_thesis_loss": 1800,
            "loss_window_seconds": 3600,
            "block_opposite_thesis": False,
            "dangerous_pairs": [],
            "medium_pair_scale": {},
            "thesis_groups": {},
        },
    }


class TestRiskPropGuardrails(unittest.TestCase):
    def test_day_reference_uses_max_balance_equity(self):
        rm = RiskManager(_base_cfg())
        ok, reason = rm.can_trade(
            open_positions=[],
            account_balance=5000.0,
            equity=5100.0,
            symbol="EURUSD",
            direction="BUY",
            setup_id="s1",
        )
        self.assertTrue(ok, msg=reason)
        self.assertAlmostEqual(rm._start_day_balance, 5000.0, places=6)
        self.assertAlmostEqual(rm._start_day_equity, 5100.0, places=6)
        self.assertAlmostEqual(rm._start_day_ref, 5100.0, places=6)
        self.assertAlmostEqual(rm._start_day_floor, 4998.0, places=6)

    def test_equity_floor_breach_blocks_entries(self):
        rm = RiskManager(_base_cfg())
        rm.can_trade(
            open_positions=[],
            account_balance=5000.0,
            equity=5100.0,
            symbol="EURUSD",
            direction="BUY",
            setup_id="s1",
        )
        ok, reason = rm.can_trade(
            open_positions=[],
            account_balance=5000.0,
            equity=4998.0,
            symbol="GBPUSD",
            direction="SELL",
            setup_id="s2",
        )
        self.assertFalse(ok)
        self.assertIn("MAX_DAILY_LOSS_EQUITY", reason)
        self.assertIn("close_all=1", reason)

    def test_double_close_idempotency_counts_once(self):
        rm = RiskManager(_base_cfg())
        ts = datetime.now(timezone.utc)
        rm.on_trade_closed(
            symbol="EURUSD",
            outcome="LOSS",
            pnl=-25.0,
            exit_time=ts,
            ticket=1001,
            direction="SELL",
            setup_id="same",
        )
        rm.on_trade_closed(
            symbol="EURUSD",
            outcome="LOSS",
            pnl=-25.0,
            exit_time=ts + timedelta(seconds=1),
            ticket=1001,
            direction="SELL",
            setup_id="same",
        )
        self.assertAlmostEqual(rm._daily_pnl, -25.0, places=6)
        self.assertIn(1001, rm._processed_closes_today)
        self.assertEqual(len(rm._processed_closes_today), 1)

    def test_correlation_same_thesis_block(self):
        rm = RiskManager(_base_cfg())
        open_positions = [{"symbol": "EURUSD", "type": "SELL"}]  # USD_LONG thesis
        ok, reason = rm.can_trade(
            open_positions=open_positions,
            account_balance=5000.0,
            equity=5000.0,
            symbol="GBPUSD",
            direction="SELL",
            setup_id="s2",
        )
        self.assertFalse(ok)
        self.assertIn("CORR_BLOCK", reason)
        self.assertIn("USD_LONG", reason)

    def test_thesis_cooldown_after_two_losses(self):
        rm = RiskManager(_base_cfg())
        now = datetime.now(timezone.utc)
        rm.on_trade_closed(
            symbol="EURUSD",
            outcome="LOSS",
            pnl=-10.0,
            exit_time=now,
            ticket=2001,
            direction="SELL",  # USD_LONG
            setup_id="a",
        )
        rm.on_trade_closed(
            symbol="USDJPY",
            outcome="LOSS",
            pnl=-10.0,
            exit_time=now + timedelta(minutes=5),
            ticket=2002,
            direction="BUY",  # USD_LONG
            setup_id="b",
        )
        ok, reason = rm.can_trade(
            open_positions=[],
            account_balance=5000.0,
            equity=5000.0,
            symbol="AUDUSD",
            direction="SELL",  # USD_LONG
            setup_id="c",
        )
        self.assertFalse(ok)
        self.assertIn("THESIS_COOLDOWN", reason)
        self.assertIn("USD_LONG", reason)

    def test_reentry_gate_blocks_same_setup_after_loss(self):
        cfg = _base_cfg()
        cfg["correlation"]["enabled"] = False
        rm = RiskManager(cfg)
        now = datetime.now(timezone.utc)
        rm.on_trade_closed(
            symbol="EURUSD",
            outcome="LOSS",
            pnl=-15.0,
            exit_time=now,
            ticket=3001,
            direction="SELL",
            setup_id="setup-A",
        )
        rm._cooldown_until_by_symbol.clear()
        rm._loss_cooldown_until_by_symbol.clear()

        blocked_ok, blocked_reason = rm.can_trade(
            open_positions=[],
            account_balance=5000.0,
            equity=5000.0,
            symbol="EURUSD",
            direction="SELL",
            setup_id="setup-A",
        )
        self.assertFalse(blocked_ok)
        self.assertIn("REENTRY_DIR_COOLDOWN", blocked_reason)

        blocked_new_ok, blocked_new_reason = rm.can_trade(
            open_positions=[],
            account_balance=5000.0,
            equity=5000.0,
            symbol="EURUSD",
            direction="SELL",
            setup_id="setup-B",
        )
        self.assertFalse(blocked_new_ok)
        self.assertIn("REENTRY_DIR_COOLDOWN", blocked_new_reason)

        key = ("EURUSD", "SELL")
        rm._symbol_dir_block_until[key] = now - timedelta(seconds=1)

        blocked_same_ok, blocked_same_reason = rm.can_trade(
            open_positions=[],
            account_balance=5000.0,
            equity=5000.0,
            symbol="EURUSD",
            direction="SELL",
            setup_id="setup-A",
        )
        self.assertFalse(blocked_same_ok)
        self.assertIn("REENTRY_SETUP_BLOCK", blocked_same_reason)

        unblocked_ok, unblocked_reason = rm.can_trade(
            open_positions=[],
            account_balance=5000.0,
            equity=5000.0,
            symbol="EURUSD",
            direction="SELL",
            setup_id="setup-B",
        )
        self.assertTrue(unblocked_ok, msg=unblocked_reason)

    def test_daily_loss_breach_sets_stop_for_day_lock(self):
        rm = RiskManager(_base_cfg())
        rm.can_trade(
            open_positions=[],
            account_balance=5000.0,
            equity=5100.0,
            symbol="EURUSD",
            direction="BUY",
            setup_id="s1",
        )
        ok1, reason1 = rm.can_trade(
            open_positions=[],
            account_balance=5000.0,
            equity=4998.0,
            symbol="GBPUSD",
            direction="SELL",
            setup_id="s2",
        )
        self.assertFalse(ok1)
        self.assertIn("MAX_DAILY_LOSS_EQUITY", reason1)
        self.assertIsNotNone(rm._prop_stop_for_day_until)

        ok2, reason2 = rm.can_trade(
            open_positions=[],
            account_balance=5000.0,
            equity=5200.0,
            symbol="USDCHF",
            direction="BUY",
            setup_id="s3",
        )
        self.assertFalse(ok2)
        self.assertIn("LOSS_STREAK_STOP_DAY", reason2)

    def test_dangerous_pair_block_same_usd_thesis(self):
        cfg = _base_cfg()
        cfg["correlation"]["max_same_thesis_open"] = 2
        cfg["correlation"]["dangerous_pairs"] = [["EURUSD", "GBPUSD"]]
        rm = RiskManager(cfg)
        open_positions = [{"symbol": "EURUSD", "type": "BUY"}]  # USD_SHORT
        ok, reason = rm.can_trade(
            open_positions=open_positions,
            account_balance=5000.0,
            equity=5000.0,
            symbol="GBPUSD",
            direction="BUY",
            setup_id="d1",
        )
        self.assertFalse(ok)
        self.assertIn("CORR_BLOCK_DANGEROUS", reason)

    def test_correlation_scale_medium_pair(self):
        cfg = _base_cfg()
        cfg["correlation"]["enabled"] = True
        cfg["correlation"]["medium_pair_scale"] = {"EURUSD|AUDUSD": 0.65}
        rm = RiskManager(cfg)
        scale, detail = rm.correlation_risk_scale(
            symbol="EURUSD",
            direction="BUY",
            open_positions=[{"symbol": "AUDUSD", "type": "BUY"}],  # same USD_SHORT thesis
        )
        self.assertAlmostEqual(scale, 0.65, places=6)
        self.assertIn("CORR_DECISION=SCALE", detail)
        self.assertIn("PAIR_TRIGGER=AUDUSD<->EURUSD", detail)

    def test_max_usd_weakness_open_blocks_third(self):
        cfg = _base_cfg()
        cfg["correlation"]["enabled"] = True
        cfg["correlation"]["max_same_thesis_open"] = 5
        cfg["correlation"]["max_usd_short_open"] = 2
        cfg["correlation"]["dangerous_pairs"] = []
        rm = RiskManager(cfg)
        open_positions = [
            {"symbol": "EURUSD", "type": "BUY"},
            {"symbol": "AUDUSD", "type": "BUY"},
        ]  # 2x USD_SHORT already
        ok, reason = rm.can_trade(
            open_positions=open_positions,
            account_balance=5000.0,
            equity=5000.0,
            symbol="GBPUSD",
            direction="BUY",
            setup_id="u3",
        )
        self.assertFalse(ok)
        self.assertIn("CORR_BLOCK_USD_WEAKNESS", reason)
        self.assertIn("THESIS=USD_SHORT", reason)

    def test_single_loss_scales_thesis_risk(self):
        cfg = _base_cfg()
        cfg["correlation"]["single_loss_risk_scale"] = 0.5
        cfg["correlation"]["single_loss_risk_scale_seconds"] = 3600
        rm = RiskManager(cfg)
        now = datetime.now(timezone.utc)
        rm.on_trade_closed(
            symbol="EURUSD",
            outcome="LOSS",
            pnl=-10.0,
            exit_time=now,
            ticket=4001,
            direction="SELL",  # USD_LONG
            setup_id="sx",
        )
        scale, detail = rm.correlation_risk_scale(
            symbol="USDJPY",
            direction="BUY",  # USD_LONG
            open_positions=[],
        )
        self.assertAlmostEqual(scale, 0.5, places=6)
        self.assertIn("THESIS=USD_LONG", detail)
        self.assertIn("CORR_DECISION=SCALE", detail)
        self.assertIn("PAIR_TRIGGER=THESIS_LOSS_SCALE", detail)

    def test_opposite_thesis_block_is_config_controlled(self):
        cfg = _base_cfg()
        cfg["correlation"]["block_opposite_thesis"] = False
        rm = RiskManager(cfg)
        ok, reason = rm.can_trade(
            open_positions=[{"symbol": "EURUSD", "type": "BUY"}],  # USD_SHORT open
            account_balance=5000.0,
            equity=5000.0,
            symbol="USDCHF",
            direction="BUY",  # candidate USD_LONG (opposite)
            setup_id="h1",
        )
        self.assertTrue(ok, msg=reason)

        cfg2 = _base_cfg()
        cfg2["correlation"]["max_same_thesis_open"] = 5
        cfg2["correlation"]["block_opposite_thesis"] = True
        rm2 = RiskManager(cfg2)
        ok2, reason2 = rm2.can_trade(
            open_positions=[{"symbol": "EURUSD", "type": "BUY"}],  # USD_SHORT open
            account_balance=5000.0,
            equity=5000.0,
            symbol="USDCHF",
            direction="BUY",  # candidate USD_LONG (opposite)
            setup_id="h2",
        )
        self.assertFalse(ok2)
        self.assertIn("CORR_BLOCK_OPPOSITE", reason2)
        self.assertIn("CORR_DECISION=BLOCK", reason2)


if __name__ == "__main__":
    unittest.main()
