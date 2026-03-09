
"""
Loss Analyzer — Deterministic Adaptive Learning System
═════════════════════════════════════════════════════
Analyzes every losing trade to find patterns and create rules that
prevent the same mistakes from repeating.

Two-phase approach:
    Phase 1 (OBSERVE):    Analyze losses, persist lessons and candidate rules,
                          but do NOT block any entries. Safe to run immediately.
    Phase 2 (CONTROLLED): Optionally block entries when adaptive rules match,
                          behind strict config gates. Only enabled when you're
                          confident the rules are well-calibrated.

How it works:
    1. After a loss, scan for OPPOSING signals that were present at entry time
       (e.g., if we went BUY but there was a bearish FVG + stop hunt forming)
    2. If 2+ opposing signals existed, create a CANDIDATE avoidance rule
    3. Over time, validate candidates: if precision > 65% with 10+ samples,
       promote to ACTIVE. Otherwise, disable.
    4. In Phase 2, ACTIVE rules can block future entries when the same
       opposing pattern appears.

Rule lifecycle: CANDIDATE → ACTIVE → DISABLED (or expired via TTL)

The system uses either SharedLearningDB (global) or TradingMemoryDB (local)
as its backing store, preferring shared when available.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("LOSS_ANALYZER")


@dataclass
class LossLesson:
    trade_id: int
    symbol: str
    expected_direction: str
    actual_direction: str
    entry_reasons: List[str]
    entry_setups_detected: List[str]
    entry_confidence: float
    missed_opposing_signals: List[str]
    strongest_opposing_setup: Optional[str]
    opposing_confluence_count: int
    lesson_summary: str
    created_at_utc: datetime
    htf_bias: str
    kill_zone: str
    spread_pips: float


@dataclass
class AdaptiveRule:
    id: int
    created_at_utc: datetime
    rule_type: str
    affected_setup: str
    check_for: str
    check_direction: str
    threshold: float
    description: str
    example: str
    active: bool
    sample_size: int
    wins_blocked_est: float
    losses_prevented_est: float
    times_triggered: int
    trades_blocked: int
    false_positives: int
    last_triggered_utc: Optional[datetime]
    expires_at_utc: Optional[datetime]
    status: str


class LossAnalyzer:
    def __init__(self, mt5_connector, strategy, memory_db, config, shared_learning_db=None):
        self.mt5 = mt5_connector
        self.strategy = strategy
        self.memory = memory_db
        self.shared = shared_learning_db
        self.cfg = config if isinstance(config, dict) else {}
        self.account_login = self._extract_account_login()

        self.al_cfg = self._build_adaptive_cfg(self.cfg)
        self.learning_store = self._resolve_learning_store()

        self.learned_lessons: List[LossLesson] = []
        self.adaptive_rules: List[AdaptiveRule] = []
        self.pattern_counter: Counter = Counter()

        self.total_losses_analyzed: int = 0
        self.rules_created: int = 0
        self.trades_blocked: int = 0

        self.load_rules_from_db()

        logger.info(
            "ADAPTIVE_LEARNING_INIT enabled=%s phase=%s entry_blocking=%s shadow_mode=%s shared_store=%s",
            int(self.al_cfg["enabled"]),
            int(self.al_cfg["phase"]),
            int(self.al_cfg["entry_blocking_enabled"]),
            int(self.al_cfg["shadow_mode"]),
            int(self.learning_store is not self.memory),
        )

    def _extract_account_login(self) -> int:
        try:
            cfg = getattr(self.mt5, "cfg", {})
            return int((cfg or {}).get("login", 0) or 0)
        except Exception:
            return 0

    def _store_has(self, store, method_name: str) -> bool:
        return bool(store is not None and callable(getattr(store, method_name, None)))

    def _resolve_learning_store(self):
        required = (
            "load_adaptive_rules",
            "save_adaptive_rule",
            "save_rule_event",
            "count_matching_lessons",
            "get_rule_events_count",
            "get_adaptive_learning_stats",
            "save_learned_lesson",
        )
        if self.shared is not None and all(self._store_has(self.shared, m) for m in required):
            return self.shared
        return self.memory

    def _build_adaptive_cfg(self, root_cfg: dict) -> dict:
        raw = root_cfg.get("adaptive_learning", {})
        if not isinstance(raw, dict):
            raw = {}
        out = {
            "enabled": bool(raw.get("enabled", True)),
            "phase": int(raw.get("phase", 1) or 1),
            "entry_blocking_enabled": bool(raw.get("entry_blocking_enabled", False)),
            "candidate_ttl_hours": int(raw.get("candidate_ttl_hours", 72) or 72),
            "min_losses_before_rule": int(raw.get("min_losses_before_rule", 5) or 5),
            "min_rule_sample_size": int(raw.get("min_rule_sample_size", 10) or 10),
            "min_rule_precision": float(raw.get("min_rule_precision", 0.65) or 0.65),
            "max_active_rules_per_setup": int(raw.get("max_active_rules_per_setup", 5) or 5),
            "cooldown_seconds_after_new_rule": int(raw.get("cooldown_seconds_after_new_rule", 1800) or 1800),
            "shadow_mode": bool(raw.get("shadow_mode", root_cfg.get("shadow_mode", False))),
        }
        return out

    def _utcnow(self) -> datetime:
        return datetime.now(timezone.utc).replace(tzinfo=None)

    def _get_value(self, obj, key: str, default=None):
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)

    def _to_direction(self, value: str) -> str:
        s = str(value or "").upper().strip()
        if s.startswith("BUY"):
            return "BUY"
        if s.startswith("SELL"):
            return "SELL"
        return s or "UNKNOWN"

    def _signal_dir_text(self, raw) -> str:
        v = raw
        if hasattr(raw, "value"):
            v = getattr(raw, "value")
        return self._to_direction(str(v or ""))

    def _parse_time(self, value) -> Optional[datetime]:
        if value is None:
            return None
        if isinstance(value, datetime):
            if value.tzinfo is not None:
                return value.astimezone(timezone.utc).replace(tzinfo=None)
            return value
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                return parsed.astimezone(timezone.utc).replace(tzinfo=None)
            return parsed
        except Exception:
            return None

    def _is_enabled(self) -> bool:
        return bool(self.al_cfg.get("enabled", False))

    def _is_phase2_blocking(self) -> bool:
        return (
            self._is_enabled()
            and int(self.al_cfg.get("phase", 1)) == 2
            and bool(self.al_cfg.get("entry_blocking_enabled", False))
        )

    def _extract_entry_reasons(self, trade_record) -> List[str]:
        reason_text = str(self._get_value(trade_record, "reason", "") or "")
        reasons: List[str] = []
        if reason_text:
            upper = reason_text.upper()
            if "FVG" in upper:
                reasons.append("FVG")
            if "ORDER_BLOCK" in upper or "OB" in upper:
                reasons.append("ORDER_BLOCK")
            if "STOP HUNT" in upper or "STOP_HUNT" in upper:
                reasons.append("STOP_HUNT")
            if "LIQUIDITY" in upper:
                reasons.append("LIQUIDITY_SWEEP")
            if "DISPLACEMENT" in upper:
                reasons.append("DISPLACEMENT")
            if "HTF" in upper and "BIAS" in upper:
                reasons.append("HTF_BIAS")
        if not reasons:
            reasons.append("UNKNOWN_REASON")
        return reasons

    def _extract_entry_setups(self, trade_record) -> List[str]:
        raw = self._get_value(trade_record, "setup_type", "")
        if not raw:
            return ["UNKNOWN"]
        setup = str(raw).strip().upper()
        return [setup] if setup else ["UNKNOWN"]

    def _find_opposing_signals(
        self,
        symbol: str,
        candles_h4: List[dict],
        candles_m15: List[dict],
        candles_m5: List[dict],
        opposite_direction: str,
    ) -> Dict:
        opposing = {
            "fvg": [],
            "stop_hunt": None,
            "order_block": None,
            "displacement": False,
            "structure_break": False,
            "liquidity_sweep": False,
        }

        try:
            fvgs = self.strategy.find_fvg(candles_m15 or [])
            for fvg in fvgs or []:
                fvg_dir = self._signal_dir_text(getattr(fvg, "direction", ""))
                if fvg_dir == opposite_direction:
                    opposing["fvg"].append(fvg)
        except Exception:
            pass

        try:
            stop_hunt_bias = "BEARISH" if opposite_direction == "SELL" else "BULLISH"
            sh = self.strategy.stop_hunt_signal(candles_m15 or [], symbol, stop_hunt_bias)
            if sh:
                opposing["stop_hunt"] = sh
        except Exception:
            pass

        try:
            obs = self.strategy.find_order_blocks(candles_m15 or [], opposite_direction)
            if obs:
                opposing["order_block"] = obs[0]
        except Exception:
            pass

        opposing["displacement"] = self._check_displacement(candles_m5 or [], opposite_direction)
        opposing["structure_break"] = self._check_structure_break(candles_h4 or [], opposite_direction)
        opposing["liquidity_sweep"] = self._check_liquidity_sweep(candles_m15 or [], opposite_direction)

        return opposing

    def _check_displacement(self, candles_m5: List[dict], direction: str) -> bool:
        if len(candles_m5) < 10:
            return False
        recent = candles_m5[-10:]
        if direction == "SELL":
            bearish = sum(1 for c in recent if float(c.get("close", 0.0)) < float(c.get("open", 0.0)))
            return bearish >= 7
        bullish = sum(1 for c in recent if float(c.get("close", 0.0)) > float(c.get("open", 0.0)))
        return bullish >= 7

    def _check_structure_break(self, candles_h4: List[dict], direction: str) -> bool:
        if len(candles_h4) < 20:
            return False
        recent = candles_h4[-20:]
        if direction == "SELL":
            swing_high = max(float(c.get("high", 0.0)) for c in recent[:-5])
            return any(float(c.get("close", 0.0)) < swing_high * 0.995 for c in recent[-5:])
        swing_low = min(float(c.get("low", 0.0)) for c in recent[:-5])
        return any(float(c.get("close", 0.0)) > swing_low * 1.005 for c in recent[-5:])

    def _check_liquidity_sweep(self, candles_m15: List[dict], direction: str) -> bool:
        if len(candles_m15) < 20:
            return False
        recent = candles_m15[-20:]
        if direction == "SELL":
            highs = [float(c.get("high", 0.0)) for c in recent[:-2]]
            if not highs:
                return False
            recent_high = max(highs)
            return any(
                float(c.get("high", 0.0)) > recent_high and float(c.get("close", 0.0)) < recent_high * 0.998
                for c in recent[-2:]
            )
        lows = [float(c.get("low", 0.0)) for c in recent[:-2]]
        if not lows:
            return False
        recent_low = min(lows)
        return any(
            float(c.get("low", 0.0)) < recent_low and float(c.get("close", 0.0)) > recent_low * 1.002
            for c in recent[-2:]
        )

    def _summarize_opposing_signals(self, opposing: Dict) -> Tuple[List[str], Optional[str], int]:
        labels: List[str] = []
        strengths: Dict[str, int] = {}

        fvg_count = len(opposing.get("fvg") or [])
        if fvg_count > 0:
            labels.append(f"FVG x{fvg_count}")
            strengths["FVG"] = fvg_count

        if opposing.get("stop_hunt"):
            labels.append("STOP_HUNT")
            strengths["STOP_HUNT"] = 1

        if opposing.get("order_block"):
            labels.append("ORDER_BLOCK")
            strengths["ORDER_BLOCK"] = 1

        if bool(opposing.get("displacement")):
            labels.append("DISPLACEMENT")
            strengths["DISPLACEMENT"] = 2

        if bool(opposing.get("structure_break")):
            labels.append("STRUCTURE_BREAK")
            strengths["STRUCTURE_BREAK"] = 2

        if bool(opposing.get("liquidity_sweep")):
            labels.append("LIQUIDITY_SWEEP")
            strengths["LIQUIDITY_SWEEP"] = 1

        confluence_count = fvg_count
        confluence_count += 1 if opposing.get("stop_hunt") else 0
        confluence_count += 1 if opposing.get("order_block") else 0
        confluence_count += 1 if opposing.get("displacement") else 0
        confluence_count += 1 if opposing.get("structure_break") else 0
        confluence_count += 1 if opposing.get("liquidity_sweep") else 0

        strongest = None
        if strengths:
            strongest = sorted(strengths.items(), key=lambda item: (item[1], item[0]), reverse=True)[0][0]

        return labels, strongest, confluence_count

    def _lesson_text(
        self,
        direction: str,
        entry_reasons: List[str],
        missed: List[str],
        strongest: Optional[str],
    ) -> str:
        missed_txt = ", ".join(missed) if missed else "none"
        strongest_txt = strongest or "NONE"
        return (
            f"Entered {direction} because {', '.join(entry_reasons)}; "
            f"missed opposing signals: {missed_txt}; strongest opposing setup: {strongest_txt}."
        )

    def _clamp(self, value: int, min_v: int, max_v: int) -> int:
        return max(min_v, min(max_v, int(value)))

    def _candidate_exists(self, setup: str, check_for: str, threshold: float) -> bool:
        setup_u = str(setup or "").upper()
        check_u = str(check_for or "").upper()
        for rule in self.adaptive_rules:
            if str(rule.affected_setup).upper() != setup_u:
                continue
            if str(rule.check_for).upper() != check_u:
                continue
            if int(float(rule.threshold)) != int(float(threshold)):
                continue
            if str(rule.status).upper() in ("CANDIDATE", "ACTIVE"):
                return True
        return False

    async def analyze_loss(self, trade_record, candles_h4, candles_m15, candles_m5) -> LossLesson:
        now = self._utcnow()
        ticket = int(self._get_value(trade_record, "ticket", 0) or 0)
        symbol = str(self._get_value(trade_record, "symbol", "") or "")
        expected_direction = self._to_direction(self._get_value(trade_record, "direction", ""))
        actual_direction = "SELL" if expected_direction == "BUY" else "BUY"

        entry_reasons = self._extract_entry_reasons(trade_record)
        entry_setups = self._extract_entry_setups(trade_record)

        opposing = self._find_opposing_signals(
            symbol=symbol,
            candles_h4=candles_h4 or [],
            candles_m15=candles_m15 or [],
            candles_m5=candles_m5 or [],
            opposite_direction=actual_direction,
        )
        missed, strongest, opposing_count = self._summarize_opposing_signals(opposing)

        lesson = LossLesson(
            trade_id=ticket,
            symbol=symbol,
            expected_direction=expected_direction,
            actual_direction=actual_direction,
            entry_reasons=entry_reasons,
            entry_setups_detected=entry_setups,
            entry_confidence=float(self._get_value(trade_record, "confidence", 0.0) or 0.0),
            missed_opposing_signals=missed,
            strongest_opposing_setup=strongest,
            opposing_confluence_count=opposing_count,
            lesson_summary=self._lesson_text(expected_direction, entry_reasons, missed, strongest),
            created_at_utc=now,
            htf_bias=str(self._get_value(trade_record, "htf_bias", "UNKNOWN") or "UNKNOWN"),
            kill_zone=str(self._get_value(trade_record, "kill_zone", "UNKNOWN") or "UNKNOWN"),
            spread_pips=float(self._get_value(trade_record, "spread_pips", 0.0) or 0.0),
        )

        self.total_losses_analyzed += 1
        self.learned_lessons.append(lesson)
        for missed_signal in lesson.missed_opposing_signals:
            self.pattern_counter[missed_signal] += 1

        logger.info(
            "LOSS_ANALYZER_RUN ticket=%s symbol=%s dir=%s missed=%s strongest=%s opposing_count=%s",
            ticket,
            symbol,
            expected_direction,
            json.dumps(missed),
            strongest or "NONE",
            opposing_count,
        )

        try:
            self.save_lesson_to_db(lesson)
        except Exception as e:
            logger.error("ADAPTIVE_LEARNING_ERROR action=save_lesson ticket=%s err=%s", ticket, e, exc_info=True)

        if self._is_enabled() and opposing_count >= 2:
            try:
                affected_setup = (entry_setups[0] if entry_setups else "UNKNOWN").upper()
                check_for = str(strongest or "UNKNOWN").upper()
                threshold = self._clamp(max(2, opposing_count), 2, 6)
                if not self._candidate_exists(affected_setup, check_for, threshold):
                    ttl_hours = int(self.al_cfg.get("candidate_ttl_hours", 72) or 72)
                    expires = now + timedelta(hours=max(1, ttl_hours))
                    candidate = AdaptiveRule(
                        id=0,
                        created_at_utc=now,
                        rule_type="AVOIDANCE",
                        affected_setup=affected_setup,
                        check_for=check_for,
                        check_direction="OPPOSITE",
                        threshold=float(threshold),
                        description=(
                            f"Before {affected_setup}, verify opposing {check_for} confluence and block when "
                            f"signals >= {threshold}."
                        ),
                        example=f"{affected_setup} entry blocked when opposing {check_for} confluence is high.",
                        active=False,
                        sample_size=0,
                        wins_blocked_est=0.0,
                        losses_prevented_est=float(opposing_count),
                        times_triggered=0,
                        trades_blocked=0,
                        false_positives=0,
                        last_triggered_utc=None,
                        expires_at_utc=expires,
                        status="CANDIDATE",
                    )
                    candidate_id = self.save_rule_to_db(candidate)
                    candidate.id = candidate_id
                    self.adaptive_rules.append(candidate)
                    self.rules_created += 1
                    logger.info(
                        "ADAPTIVE_RULE_CANDIDATE_CREATED rule_id=%s setup=%s check_for=%s threshold=%.2f expires=%s",
                        candidate.id,
                        candidate.affected_setup,
                        candidate.check_for,
                        candidate.threshold,
                        candidate.expires_at_utc.isoformat() if candidate.expires_at_utc else "NONE",
                    )
            except Exception as e:
                logger.error("ADAPTIVE_LEARNING_ERROR action=create_candidate ticket=%s err=%s", ticket, e, exc_info=True)

        return lesson

    def load_rules_from_db(self) -> List[AdaptiveRule]:
        self.adaptive_rules = []
        store = self.learning_store
        if not self._store_has(store, "load_adaptive_rules"):
            return self.adaptive_rules
        try:
            rows = store.load_adaptive_rules() or []
            for row in rows:
                created = self._parse_time(row.get("created_at_utc")) or self._utcnow()
                last_triggered = self._parse_time(row.get("last_triggered_utc"))
                expires = self._parse_time(row.get("expires_at_utc"))
                self.adaptive_rules.append(
                    AdaptiveRule(
                        id=int(row.get("id") or 0),
                        created_at_utc=created,
                        rule_type=str(row.get("rule_type") or ""),
                        affected_setup=str(row.get("affected_setup") or ""),
                        check_for=str(row.get("check_for") or ""),
                        check_direction=str(row.get("check_direction") or ""),
                        threshold=float(row.get("threshold") or 0.0),
                        description=str(row.get("description") or ""),
                        example=str(row.get("example") or ""),
                        active=bool(row.get("active", False)),
                        sample_size=int(row.get("sample_size") or 0),
                        wins_blocked_est=float(row.get("wins_blocked_est") or 0.0),
                        losses_prevented_est=float(row.get("losses_prevented_est") or 0.0),
                        times_triggered=int(row.get("times_triggered") or 0),
                        trades_blocked=int(row.get("trades_blocked") or 0),
                        false_positives=int(row.get("false_positives") or 0),
                        last_triggered_utc=last_triggered,
                        expires_at_utc=expires,
                        status=str(row.get("status") or "CANDIDATE").upper(),
                    )
                )
        except Exception as e:
            logger.error("ADAPTIVE_LEARNING_ERROR action=load_rules err=%s", e, exc_info=True)
        return list(self.adaptive_rules)

    def save_lesson_to_db(self, lesson: LossLesson) -> int:
        payload = {
            "ticket": lesson.trade_id,
            "symbol": lesson.symbol,
            "expected_direction": lesson.expected_direction,
            "actual_direction": lesson.actual_direction,
            "entry_reasons_json": json.dumps(lesson.entry_reasons),
            "entry_setups_json": json.dumps(lesson.entry_setups_detected),
            "entry_confidence": lesson.entry_confidence,
            "missed_opposing_signals_json": json.dumps(lesson.missed_opposing_signals),
            "strongest_opposing_setup": lesson.strongest_opposing_setup,
            "opposing_confluence_count": lesson.opposing_confluence_count,
            "lesson_summary": lesson.lesson_summary,
            "created_at_utc": lesson.created_at_utc,
            "htf_bias": lesson.htf_bias,
            "kill_zone": lesson.kill_zone,
            "spread_pips": lesson.spread_pips,
            "source_account_login": self.account_login,
        }
        local_id = 0
        shared_id = 0
        if self._store_has(self.memory, "save_learned_lesson"):
            local_id = int(self.memory.save_learned_lesson(payload) or 0)
        if self.learning_store is not self.memory and self._store_has(self.learning_store, "save_learned_lesson"):
            shared_id = int(self.learning_store.save_learned_lesson(payload) or 0)
        return int(shared_id or local_id or 0)

    def save_rule_to_db(self, rule: AdaptiveRule) -> int:
        if not self._store_has(self.learning_store, "save_adaptive_rule"):
            return 0
        payload = {
            "id": rule.id,
            "created_at_utc": rule.created_at_utc,
            "rule_type": rule.rule_type,
            "affected_setup": rule.affected_setup,
            "check_for": rule.check_for,
            "check_direction": rule.check_direction,
            "threshold": rule.threshold,
            "description": rule.description,
            "example": rule.example,
            "active": rule.active,
            "sample_size": rule.sample_size,
            "wins_blocked_est": rule.wins_blocked_est,
            "losses_prevented_est": rule.losses_prevented_est,
            "times_triggered": rule.times_triggered,
            "trades_blocked": rule.trades_blocked,
            "false_positives": rule.false_positives,
            "last_triggered_utc": rule.last_triggered_utc,
            "expires_at_utc": rule.expires_at_utc,
            "status": rule.status,
            "source_account_login": self.account_login,
        }
        rid = int(self.learning_store.save_adaptive_rule(payload) or 0)
        return rid

    def _record_rule_event(
        self,
        rule_id: int,
        symbol: str,
        setup_id: str,
        direction: str,
        decision: str,
        notes: str,
    ):
        if not self._store_has(self.learning_store, "save_rule_event"):
            return
        try:
            self.learning_store.save_rule_event(
                {
                    "rule_id": int(rule_id),
                    "event_time_utc": self._utcnow(),
                    "symbol": str(symbol or ""),
                    "setup_id": str(setup_id or ""),
                    "direction": str(direction or ""),
                    "decision": str(decision or ""),
                    "notes": str(notes or ""),
                    "account_login": self.account_login,
                }
            )
        except Exception as e:
            logger.error("ADAPTIVE_LEARNING_ERROR action=save_rule_event rule_id=%s err=%s", rule_id, e, exc_info=True)

    def _rule_precision(self, rule: AdaptiveRule) -> float:
        prevented = max(0.0, float(rule.losses_prevented_est) - float(rule.false_positives))
        denominator = max(1, int(rule.trades_blocked or 0))
        if denominator <= 0:
            denominator = max(1, int(rule.sample_size or 0))
        return prevented / float(max(1, denominator))

    def _rule_matches_entry(self, rule: AdaptiveRule, setup_type: str) -> bool:
        return str(rule.affected_setup or "").upper() == str(setup_type or "").upper()

    def _rule_expired(self, rule: AdaptiveRule, now: datetime) -> bool:
        return isinstance(rule.expires_at_utc, datetime) and now >= rule.expires_at_utc

    def _rule_triggers(self, rule: AdaptiveRule, opposing: Dict) -> Tuple[bool, int, bool]:
        opposing_count = 0
        if opposing.get("fvg"):
            opposing_count += len(opposing["fvg"])
        opposing_count += 1 if opposing.get("stop_hunt") else 0
        opposing_count += 1 if opposing.get("order_block") else 0
        opposing_count += 1 if opposing.get("displacement") else 0
        opposing_count += 1 if opposing.get("structure_break") else 0
        opposing_count += 1 if opposing.get("liquidity_sweep") else 0

        check_for = str(rule.check_for or "").upper()
        check_hit = False
        if check_for in ("", "UNKNOWN"):
            check_hit = True
        elif check_for == "FVG":
            check_hit = len(opposing.get("fvg") or []) > 0
        elif check_for == "STOP_HUNT":
            check_hit = bool(opposing.get("stop_hunt"))
        elif check_for == "ORDER_BLOCK":
            check_hit = bool(opposing.get("order_block"))
        elif check_for == "DISPLACEMENT":
            check_hit = bool(opposing.get("displacement"))
        elif check_for == "STRUCTURE_BREAK":
            check_hit = bool(opposing.get("structure_break"))
        elif check_for == "LIQUIDITY_SWEEP":
            check_hit = bool(opposing.get("liquidity_sweep"))

        return opposing_count >= int(rule.threshold), opposing_count, check_hit

    def should_block_entry(
        self,
        symbol,
        setup_type,
        direction,
        candles_h4,
        candles_m15,
        candles_m5,
        setup_id: str = "",
    ) -> Tuple[bool, str]:
        if not self._is_enabled():
            return False, "ADAPTIVE_DISABLED"

        if not self._is_phase2_blocking():
            return False, "ADAPTIVE_OBSERVE_ONLY"

        now = self._utcnow()
        entry_dir = self._to_direction(direction)
        opposite_dir = "SELL" if entry_dir == "BUY" else "BUY"

        setup_rules = [
            r
            for r in self.adaptive_rules
            if r.active
            and str(r.status).upper() == "ACTIVE"
            and not self._rule_expired(r, now)
            and self._rule_matches_entry(r, setup_type)
        ]
        if not setup_rules:
            return False, "OK"

        max_active = max(1, int(self.al_cfg.get("max_active_rules_per_setup", 5) or 5))
        setup_rules = sorted(
            setup_rules,
            key=lambda r: (
                self._rule_precision(r),
                int(r.sample_size or 0),
                -int(r.id or 0),
            ),
            reverse=True,
        )[:max_active]

        opposing = self._find_opposing_signals(
            symbol=str(symbol or ""),
            candles_h4=candles_h4 or [],
            candles_m15=candles_m15 or [],
            candles_m5=candles_m5 or [],
            opposite_direction=opposite_dir,
        )

        for rule in setup_rules:
            cooldown_seconds = int(self.al_cfg.get("cooldown_seconds_after_new_rule", 1800) or 1800)
            age_seconds = (now - rule.created_at_utc).total_seconds()
            if cooldown_seconds > 0 and age_seconds < cooldown_seconds:
                continue

            threshold_hit, opposing_count, check_hit = self._rule_triggers(rule, opposing)
            if not (threshold_hit and check_hit):
                continue

            rule.times_triggered += 1
            rule.sample_size += 1
            rule.last_triggered_utc = now

            trigger_notes = (
                f"rule_id={rule.id} setup={setup_type} opposing_count={opposing_count} "
                f"threshold={rule.threshold:.2f} check_for={rule.check_for}"
            )
            self._record_rule_event(rule.id, symbol, setup_id, direction, "TRIGGERED", trigger_notes)

            if bool(self.al_cfg.get("shadow_mode", False)):
                self.save_rule_to_db(rule)
                self._record_rule_event(rule.id, symbol, setup_id, direction, "OVERRIDDEN", "WOULD_BLOCK shadow_mode=1")
                logger.info("ADAPTIVE_SHADOW_WOULD_BLOCK %s", trigger_notes)
                return False, f"WOULD_BLOCK rule_id={rule.id}"

            rule.trades_blocked += 1
            rule.losses_prevented_est += 1.0
            self.trades_blocked += 1
            self.save_rule_to_db(rule)
            self._record_rule_event(rule.id, symbol, setup_id, direction, "BLOCKED", trigger_notes)
            reason = f"ADAPTIVE_BLOCK rule_id={rule.id} setup={setup_type} check_for={rule.check_for}"
            logger.warning(reason)
            return True, reason

        return False, "OK"

    def validate_rules_job(self):
        if not self._is_enabled():
            return

        now = self._utcnow()
        min_losses = max(1, int(self.al_cfg.get("min_losses_before_rule", 5) or 5))
        min_sample = max(1, int(self.al_cfg.get("min_rule_sample_size", 10) or 10))
        min_precision = float(self.al_cfg.get("min_rule_precision", 0.65) or 0.65)
        max_active = max(1, int(self.al_cfg.get("max_active_rules_per_setup", 5) or 5))

        self.load_rules_from_db()

        changed_rules: Dict[int, AdaptiveRule] = {}
        for rule in self.adaptive_rules:
            if self._rule_expired(rule, now) and str(rule.status).upper() != "DISABLED":
                rule.status = "DISABLED"
                rule.active = False
                changed_rules[rule.id] = rule
                self._record_rule_event(rule.id, "*", "", "", "DISABLED_AUTO", "expired")
                continue

            if self._store_has(self.learning_store, "count_matching_lessons"):
                matching_losses = int(self.learning_store.count_matching_lessons(rule.affected_setup, rule.check_for) or 0)
            else:
                matching_losses = 0
            if self._store_has(self.learning_store, "get_rule_events_count"):
                event_count = int(self.learning_store.get_rule_events_count(rule.id) or 0)
            else:
                event_count = 0

            rule.sample_size = max(int(rule.sample_size or 0), matching_losses, event_count)
            if rule.losses_prevented_est <= 0 and matching_losses > 0:
                rule.losses_prevented_est = float(matching_losses)

            status = str(rule.status or "").upper()
            precision = self._rule_precision(rule)

            if status == "CANDIDATE":
                if matching_losses >= min_losses and rule.sample_size >= min_sample:
                    if precision >= min_precision:
                        rule.status = "ACTIVE"
                        rule.active = True
                    else:
                        rule.status = "DISABLED"
                        rule.active = False
                        self._record_rule_event(
                            rule.id,
                            "*",
                            "",
                            "",
                            "DISABLED_AUTO",
                            f"low_precision={precision:.3f}",
                        )
                    changed_rules[rule.id] = rule
            elif status == "ACTIVE":
                if rule.sample_size >= min_sample and precision < min_precision:
                    rule.status = "DISABLED"
                    rule.active = False
                    changed_rules[rule.id] = rule
                    self._record_rule_event(
                        rule.id,
                        "*",
                        "",
                        "",
                        "DISABLED_AUTO",
                        f"low_precision={precision:.3f}",
                    )

        active_by_setup: Dict[str, List[AdaptiveRule]] = {}
        for rule in self.adaptive_rules:
            if not rule.active or str(rule.status).upper() != "ACTIVE":
                continue
            setup = str(rule.affected_setup or "").upper()
            active_by_setup.setdefault(setup, []).append(rule)

        for setup, rules in active_by_setup.items():
            ranked = sorted(
                rules,
                key=lambda r: (self._rule_precision(r), int(r.sample_size or 0), -int(r.id or 0)),
                reverse=True,
            )
            keep_ids = {r.id for r in ranked[:max_active]}
            for rule in ranked[max_active:]:
                if rule.id in keep_ids:
                    continue
                rule.active = False
                rule.status = "DISABLED"
                changed_rules[rule.id] = rule
                self._record_rule_event(rule.id, "*", "", "", "DISABLED_AUTO", f"max_active_per_setup={setup}")

        for rule in changed_rules.values():
            self.save_rule_to_db(rule)

        if changed_rules:
            logger.info(
                "ADAPTIVE_RULE_VALIDATION updated=%s active=%s candidates=%s disabled=%s",
                len(changed_rules),
                len([r for r in self.adaptive_rules if r.active and str(r.status).upper() == "ACTIVE"]),
                len([r for r in self.adaptive_rules if str(r.status).upper() == "CANDIDATE"]),
                len([r for r in self.adaptive_rules if str(r.status).upper() == "DISABLED"]),
            )

    def get_learning_stats(self) -> Dict:
        db_stats = {}
        if self._store_has(self.learning_store, "get_adaptive_learning_stats"):
            try:
                db_stats = dict(self.learning_store.get_adaptive_learning_stats() or {})
            except Exception:
                db_stats = {}
        if self.learning_store is not self.memory and self._store_has(self.memory, "get_adaptive_learning_stats"):
            try:
                local_stats = dict(self.memory.get_adaptive_learning_stats() or {})
                for key, value in local_stats.items():
                    db_stats[f"account_{key}"] = value
            except Exception:
                pass

        return {
            "enabled": self._is_enabled(),
            "phase": int(self.al_cfg.get("phase", 1)),
            "entry_blocking_enabled": bool(self.al_cfg.get("entry_blocking_enabled", False)),
            "shadow_mode": bool(self.al_cfg.get("shadow_mode", False)),
            "learning_scope": "shared" if self.learning_store is not self.memory else "account",
            "total_losses_analyzed": int(self.total_losses_analyzed),
            "lessons_in_memory": len(self.learned_lessons),
            "rules_in_memory": len(self.adaptive_rules),
            "rules_active_in_memory": len([r for r in self.adaptive_rules if r.active and str(r.status).upper() == "ACTIVE"]),
            "trades_blocked": int(self.trades_blocked),
            **db_stats,
        }
