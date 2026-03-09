import asyncio
import os
import sys
import tempfile
import unittest
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from loss_analyzer import LossAnalyzer
from trade_analyzer import TradeAnalyzer
from trading_memory import TradingMemoryDB


@dataclass
class _Dir:
    value: str


@dataclass
class _FVG:
    direction: _Dir


class _StrategyForLoss:
    def find_fvg(self, _candles):
        return [_FVG(_Dir("SELL"))]

    def stop_hunt_signal(self, _candles, _symbol, _bias):
        return None

    def find_order_blocks(self, _candles, direction):
        return [{"direction": direction}]


class TestAdaptiveLearningPhase1(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(prefix="al_phase1_", suffix=".db")
        os.close(fd)
        self.db_path = Path(path)
        self.memory = TradingMemoryDB(self.db_path)
        self.cfg = {
            "adaptive_learning": {
                "enabled": True,
                "phase": 1,
                "entry_blocking_enabled": False,
                "candidate_ttl_hours": 72,
                "min_losses_before_rule": 5,
                "min_rule_sample_size": 10,
                "min_rule_precision": 0.65,
                "max_active_rules_per_setup": 5,
                "cooldown_seconds_after_new_rule": 1800,
                "shadow_mode": False,
            }
        }
        self.analyzer = LossAnalyzer(None, _StrategyForLoss(), self.memory, self.cfg)

    def tearDown(self):
        try:
            self.memory.close()
        finally:
            if self.db_path.exists():
                self.db_path.unlink()

    def test_analyze_loss_persists_lesson(self):
        trade = SimpleNamespace(
            ticket=1001,
            symbol="EURUSD",
            direction="BUY",
            setup_type="FVG",
            reason="FVG entry",
            confidence=0.72,
            htf_bias="BULLISH",
            kill_zone="LONDON_OPEN",
            spread_pips=1.2,
        )
        asyncio.run(self.analyzer.analyze_loss(trade, [], [], []))
        stats = self.memory.get_adaptive_learning_stats()
        self.assertEqual(int(stats.get("lessons_count", 0)), 1)

    def test_candidate_rule_created_with_status_and_expiry(self):
        trade = SimpleNamespace(
            ticket=1002,
            symbol="EURUSD",
            direction="BUY",
            setup_type="FVG",
            reason="FVG entry",
            confidence=0.70,
            htf_bias="BULLISH",
            kill_zone="NY_OPEN",
            spread_pips=1.1,
        )
        asyncio.run(self.analyzer.analyze_loss(trade, [], [], []))
        rules = self.memory.load_adaptive_rules()
        self.assertGreaterEqual(len(rules), 1)
        first = rules[0]
        self.assertEqual(str(first.get("status")), "CANDIDATE")
        self.assertFalse(bool(first.get("active")))
        self.assertIsNotNone(first.get("expires_at_utc"))

    def test_load_rules_from_db_roundtrip(self):
        trade = SimpleNamespace(
            ticket=1003,
            symbol="GBPUSD",
            direction="BUY",
            setup_type="FVG",
            reason="FVG entry",
            confidence=0.75,
            htf_bias="BULLISH",
            kill_zone="NY_OPEN",
            spread_pips=1.0,
        )
        asyncio.run(self.analyzer.analyze_loss(trade, [], [], []))
        loaded = self.analyzer.load_rules_from_db()
        self.assertGreaterEqual(len(loaded), 1)
        self.assertEqual(str(loaded[0].status), "CANDIDATE")

    def test_loss_analyzer_failure_does_not_break_close_processing(self):
        engine = SimpleNamespace()
        engine.mt5 = Mock()
        engine.memory = Mock()
        engine.brain = Mock()
        engine.cooldowns = Mock()
        engine.hybrid_gate = Mock()
        engine.risk = Mock()
        engine.loss_analyzer = Mock()
        engine.loss_analyzer.analyze_loss = Mock(side_effect=RuntimeError("boom"))

        engine.mt5.get_candles.return_value = []
        engine.memory.record_exit.return_value = True
        engine.brain.analyze_exit.return_value = None
        engine.brain.get_adaptive_confidence.return_value = 70.0
        engine.brain.should_disable_setup.return_value = False

        analyzer = TradeAnalyzer(engine)
        mt5_trade = {
            "position_id": 5551,
            "order_ticket": 5551,
            "deal_ticket": 7777,
            "price": 1.1010,
            "profit": -25.0,
            "time": datetime.now(timezone.utc).isoformat(),
        }
        db_record = {
            "id": 1,
            "ticket": 5551,
            "symbol": "EURUSD",
            "direction": "BUY",
            "setup_type": "FVG",
            "entry_price": 1.1020,
            "sl_price": 1.1000,
            "tp_price": 1.1060,
            "reason": "FVG entry",
            "confidence_input": 0.7,
            "htf_bias": "BULLISH",
            "kill_zone": "NY_OPEN",
            "spread_pips": 1.2,
        }

        asyncio.run(analyzer._analyze_closed_trade(mt5_trade, db_record))
        self.assertTrue(engine.memory.record_exit.called)
        self.assertTrue(engine.brain.analyze_exit.called)


if __name__ == "__main__":
    unittest.main()
