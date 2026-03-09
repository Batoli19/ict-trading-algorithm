import os
import sys
import tempfile
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from loss_analyzer import AdaptiveRule, LossAnalyzer
from trading_memory import TradingMemoryDB


@dataclass
class _Dir:
    value: str


@dataclass
class _FVG:
    direction: _Dir


class _StrategyAlwaysOpposing:
    def __init__(self, opposing_direction: str = "SELL"):
        self.opposing_direction = opposing_direction

    def find_fvg(self, _candles):
        return [_FVG(_Dir(self.opposing_direction))]

    def stop_hunt_signal(self, _candles, _symbol, _bias):
        return {"ok": True}

    def find_order_blocks(self, _candles, direction):
        return [{"direction": direction}]


class TestAdaptiveLearningPhase2(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(prefix="al_phase2_", suffix=".db")
        os.close(fd)
        self.db_path = Path(path)
        self.memory = TradingMemoryDB(self.db_path)

    def tearDown(self):
        try:
            self.memory.close()
        finally:
            if self.db_path.exists():
                self.db_path.unlink()

    def _cfg(self, entry_blocking=True, shadow=False):
        return {
            "adaptive_learning": {
                "enabled": True,
                "phase": 2,
                "entry_blocking_enabled": bool(entry_blocking),
                "candidate_ttl_hours": 72,
                "min_losses_before_rule": 2,
                "min_rule_sample_size": 3,
                "min_rule_precision": 0.65,
                "max_active_rules_per_setup": 5,
                "cooldown_seconds_after_new_rule": 0,
                "shadow_mode": bool(shadow),
            }
        }

    def _active_rule(self, rid: int = 1, expired=False) -> AdaptiveRule:
        now = datetime.now(timezone.utc)
        return AdaptiveRule(
            id=rid,
            created_at_utc=now - timedelta(hours=2),
            rule_type="AVOIDANCE",
            affected_setup="FVG",
            check_for="FVG",
            check_direction="OPPOSITE",
            threshold=2.0,
            description="block high opposing confluence",
            example="example",
            active=True,
            sample_size=5,
            wins_blocked_est=0.0,
            losses_prevented_est=4.0,
            times_triggered=0,
            trades_blocked=0,
            false_positives=0,
            last_triggered_utc=None,
            expires_at_utc=(now - timedelta(minutes=1)) if expired else (now + timedelta(hours=4)),
            status="ACTIVE",
        )

    def test_entry_blocking_off_never_blocks(self):
        la = LossAnalyzer(None, _StrategyAlwaysOpposing(), self.memory, self._cfg(entry_blocking=False))
        la.adaptive_rules = [self._active_rule()]
        blocked, reason = la.should_block_entry("EURUSD", "FVG", "BUY", [], [], [], setup_id="s1")
        self.assertFalse(blocked)
        self.assertEqual(reason, "ADAPTIVE_OBSERVE_ONLY")

    def test_active_rule_blocks_when_conditions_met(self):
        la = LossAnalyzer(None, _StrategyAlwaysOpposing(), self.memory, self._cfg(entry_blocking=True))
        rule = self._active_rule(rid=11)
        la.save_rule_to_db(rule)
        la.load_rules_from_db()
        blocked, reason = la.should_block_entry("EURUSD", "FVG", "BUY", [], [], [], setup_id="s2")
        self.assertTrue(blocked)
        self.assertIn("rule_id=11", reason)

    def test_candidate_rule_never_blocks(self):
        la = LossAnalyzer(None, _StrategyAlwaysOpposing(), self.memory, self._cfg(entry_blocking=True))
        now = datetime.now(timezone.utc)
        candidate = AdaptiveRule(
            id=0,
            created_at_utc=now - timedelta(hours=1),
            rule_type="AVOIDANCE",
            affected_setup="FVG",
            check_for="FVG",
            check_direction="OPPOSITE",
            threshold=2.0,
            description="candidate",
            example="candidate",
            active=False,
            sample_size=0,
            wins_blocked_est=0.0,
            losses_prevented_est=0.0,
            times_triggered=0,
            trades_blocked=0,
            false_positives=0,
            last_triggered_utc=None,
            expires_at_utc=now + timedelta(hours=3),
            status="CANDIDATE",
        )
        la.save_rule_to_db(candidate)
        la.load_rules_from_db()
        blocked, reason = la.should_block_entry("EURUSD", "FVG", "BUY", [], [], [], setup_id="s3")
        self.assertFalse(blocked)
        self.assertEqual(reason, "OK")

    def test_rule_expiry_prevents_block(self):
        la = LossAnalyzer(None, _StrategyAlwaysOpposing(), self.memory, self._cfg(entry_blocking=True))
        expired = self._active_rule(rid=12, expired=True)
        la.save_rule_to_db(expired)
        la.load_rules_from_db()
        blocked, reason = la.should_block_entry("EURUSD", "FVG", "BUY", [], [], [], setup_id="s4")
        self.assertFalse(blocked)
        self.assertEqual(reason, "OK")

    def test_validate_auto_disables_low_precision_rule(self):
        la = LossAnalyzer(None, _StrategyAlwaysOpposing(), self.memory, self._cfg(entry_blocking=True))
        weak = self._active_rule(rid=21, expired=False)
        weak.sample_size = 10
        weak.trades_blocked = 10
        weak.losses_prevented_est = 2.0
        weak.false_positives = 3
        la.save_rule_to_db(weak)
        la.load_rules_from_db()

        la.validate_rules_job()
        rows = self.memory.load_adaptive_rules()
        target = [r for r in rows if int(r.get("id")) == 21][0]
        self.assertEqual(str(target.get("status")).upper(), "DISABLED")
        self.assertFalse(bool(target.get("active")))


if __name__ == "__main__":
    unittest.main()
