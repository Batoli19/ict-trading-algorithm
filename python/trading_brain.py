"""
Trading Brain — AI Learning System
═══════════════════════════════════
The "intelligence layer" that learns from every trade the bot takes.

Key responsibilities:
    1. ENTRY ANALYSIS:  Before taking a trade, analyze WHY we should take it.
       Examines H4 trend, M15 structure (premium/discount zone), and setup-
       specific conditions. This reasoning is stored in the trade memory.

    2. EXIT ANALYSIS:   After a trade closes, determine WHY it won or lost.
       Checks for wicks/spikes, counter-trend momentum, abnormal volume.
       Generates specific lessons (e.g., "consider wider stop near news").

    3. ADAPTIVE CONFIDENCE: Adjusts confidence based on REAL historical
       win rates. If a setup starts losing more often, confidence drops.
       If a single loss pattern dominates (>50% of losses), extra penalty.

    4. SETUP DISABLING:  If a setup has 50+ trades with <60% confidence,
       the brain can recommend disabling it entirely.

Data source: TradingMemoryDB (SQLite) — all analysis reads from trade history.
"""

import logging
from typing import Dict, List, Optional
from datetime import datetime

logger = logging.getLogger("BRAIN")


class TradingBrain:
    """
    The bot's learning and analysis engine.

    Called at two key points:
        1. BEFORE entry: analyze_entry_conditions() provides reasoning
        2. AFTER exit:   analyze_exit() determines why the trade won/lost

    Also provides adaptive confidence via get_adaptive_confidence().
    """

    def __init__(self, memory_db, config: Optional[dict] = None):
        """
        Args:
            memory_db: TradingMemoryDB instance for reading trade history
            config:    Full bot config dict (used for force_enable_setups)
        """
        self.memory = memory_db
        self.config = config or {}
        
    # ── Analyze Trade Entry ──────────────────────────────────────────────────
    def analyze_entry_conditions(self, symbol: str, setup_type: str, 
                                  candles_h4: list, candles_m15: list,
                                  candles_m5: list, signal) -> Dict:
        """
        Deep analysis of WHY we're taking this trade.
        Returns detailed reasoning and conditions met.
        """
        conditions_met = []
        reasoning_parts = []
        
        # ─── H4 Trend Analysis ────────────────────────────────────────
        # Count bullish vs bearish candles in last 10 H4 bars.
        # 7+ in one direction = strong trend (ICT "HTF bias").
        if len(candles_h4) >= 30:
            recent_h4 = candles_h4[-10:]
            trend_up = sum(1 for c in recent_h4 if c["close"] > c["open"]) > 6
            trend_down = sum(1 for c in recent_h4 if c["close"] < c["open"]) > 6
            
            if trend_up:
                conditions_met.append("H4 uptrend (7+ bullish candles in last 10)")
                reasoning_parts.append("Strong H4 bullish momentum")
            elif trend_down:
                conditions_met.append("H4 downtrend (7+ bearish candles in last 10)")
                reasoning_parts.append("Strong H4 bearish momentum")
            else:
                conditions_met.append("H4 ranging/choppy")
                reasoning_parts.append("Neutral H4, relying on M15 structure")
        
        # ─── M15 Premium/Discount Zone Analysis ──────────────────────
        # ICT concept: lower 30% of range = discount (buy), upper 30% = premium (sell)
        if len(candles_m15) >= 20:
            recent_m15 = candles_m15[-20:]
            swing_high = max(c["high"] for c in recent_m15)
            swing_low = min(c["low"] for c in recent_m15)
            current_price = recent_m15[-1]["close"]
            
            # Premium vs Discount
            range_size = swing_high - swing_low
            from_low = current_price - swing_low
            position_in_range = (from_low / range_size * 100) if range_size > 0 else 50
            
            if position_in_range < 30:
                conditions_met.append(f"Price in discount zone ({position_in_range:.0f}% of range)")
                reasoning_parts.append("Buying at discount (lower 30%)")
            elif position_in_range > 70:
                conditions_met.append(f"Price in premium zone ({position_in_range:.0f}% of range)")
                reasoning_parts.append("Selling at premium (upper 30%)")
            else:
                conditions_met.append(f"Price in equilibrium ({position_in_range:.0f}% of range)")
        
        # ─── Setup-Specific Reasoning ─────────────────────────────────
        # Each ICT setup has a different thesis for why the trade works
        if setup_type == "FVG":
            conditions_met.append("Fair Value Gap identified on M15")
            reasoning_parts.append("FVG provides imbalance to fill")
        
        elif setup_type == "SNIPER":
            conditions_met.append("Sniper entry at premium/discount with rejection")
            reasoning_parts.append("High-probability precision entry with tight stop")
        
        elif setup_type == "STOP_HUNT":
            conditions_met.append("Equal highs/lows swept + displacement")
            reasoning_parts.append("Liquidity grab followed by strong move")
        
        elif setup_type == "ENGULFING":
            last_candle = candles_m5[-1] if candles_m5 else None
            if last_candle:
                body_size = abs(last_candle["close"] - last_candle["open"])
                conditions_met.append(f"Engulfing pattern (body: {body_size:.5f})")
                reasoning_parts.append("Strong rejection candle showing momentum shift")
        
        elif setup_type == "MANIPULATION":
            conditions_met.append("Counter-trend manipulation exhaustion detected")
            reasoning_parts.append("Fake-out move exhausted, reversing to HTF bias")
        
        # Build full reasoning
        full_reasoning = " | ".join(reasoning_parts) if reasoning_parts else "Standard setup conditions met"
        
        # Expected outcome
        sl_pips = abs(signal.entry - signal.sl) / self._get_pip_size(symbol)
        tp_pips = abs(signal.tp - signal.entry) / self._get_pip_size(symbol)
        rr = tp_pips / sl_pips if sl_pips > 0 else 0
        
        expected_outcome = (
            f"Expecting {signal.direction.value} continuation. "
            f"SL: {sl_pips:.1f}p, TP: {tp_pips:.1f}p, RR: 1:{rr:.1f}. "
            f"HTF bias supports direction."
        )
        
        return {
            'conditions_met': conditions_met,
            'reasoning': full_reasoning,
            'expected_outcome': expected_outcome
        }
    
    # ── Analyze Trade Exit ───────────────────────────────────────────────────
    def analyze_exit(self, trade_record: Dict, candles_m5: list) -> Dict:
        """
        Analyze WHY the trade won or lost.
        Provides specific reasons and lessons.
        """
        outcome = trade_record['outcome']
        entry = trade_record['entry_price']
        sl = trade_record['sl_price']
        tp = trade_record['tp_price']
        exit_price = trade_record['exit_price']
        direction = trade_record['direction']
        
        analysis = {
            'stop_hit_reason': None,
            'tp_hit_reason': None,
            'lessons_learned': None
        }
        
        # Determine what happened
        hit_sl = False
        hit_tp = False
        
        if direction == "BUY":
            hit_sl = exit_price <= sl * 1.0005  # Within 0.05%
            hit_tp = exit_price >= tp * 0.9995
        else:  # SELL
            hit_sl = exit_price >= sl * 0.9995
            hit_tp = exit_price <= tp * 1.0005
        
        # LOSS Analysis
        if outcome == "LOSS":
            if hit_sl:
                # Analyze candles to see WHY stop was hit
                reasons = []
                
                if candles_m5 and len(candles_m5) >= 5:
                    recent = candles_m5[-5:]
                    
                    # Check for spike/wick
                    for candle in recent:
                        body = abs(candle["close"] - candle["open"])
                        full_range = candle["high"] - candle["low"]
                        wick_ratio = (full_range - body) / full_range if full_range > 0 else 0
                        
                        if wick_ratio > 0.6:
                            reasons.append("Large wick/spike hit stop")
                            break
                    
                    # Check for strong counter-trend move
                    if direction == "BUY":
                        bearish_count = sum(1 for c in recent if c["close"] < c["open"])
                        if bearish_count >= 4:
                            reasons.append("Strong bearish rejection (4+ red candles)")
                    else:
                        bullish_count = sum(1 for c in recent if c["close"] > c["open"])
                        if bullish_count >= 4:
                            reasons.append("Strong bullish rejection (4+ green candles)")
                    
                    # Check for news spike (if volume abnormal)
                    avg_volume = sum(c["volume"] for c in recent[:3]) / 3
                    if recent[-1]["volume"] > avg_volume * 2:
                        reasons.append("Abnormal volume spike (possible news)")
                
                if not reasons:
                    reasons.append("Stop hit - market moved against us")
                
                analysis['stop_hit_reason'] = " | ".join(reasons)
                
                # Lessons
                lessons = []
                if "spike" in analysis['stop_hit_reason'].lower():
                    lessons.append("Consider wider stop or avoid trading near news")
                if "rejection" in analysis['stop_hit_reason'].lower():
                    lessons.append("HTF bias may have been wrong - wait for stronger confirmation")
                if "volume spike" in analysis['stop_hit_reason'].lower():
                    lessons.append("Add news filter or pause trading during high-impact events")
                
                if not lessons:
                    lessons.append("Review entry criteria - may have been early or against structure")
                
                analysis['lessons_learned'] = " | ".join(lessons)
            
            else:
                # Manual close or other exit
                analysis['stop_hit_reason'] = "Exited before SL hit (trailing stop or manual close)"
                analysis['lessons_learned'] = "Acceptable loss - risk managed properly"
        
        # WIN Analysis
        elif outcome == "WIN":
            if hit_tp:
                reasons = []
                
                if candles_m5 and len(candles_m5) >= 5:
                    recent = candles_m5[-5:]
                    
                    # Check for clean move
                    if direction == "BUY":
                        bullish_count = sum(1 for c in recent if c["close"] > c["open"])
                        if bullish_count >= 4:
                            reasons.append("Clean bullish momentum (4+ green candles)")
                    else:
                        bearish_count = sum(1 for c in recent if c["close"] < c["open"])
                        if bearish_count >= 4:
                            reasons.append("Clean bearish momentum (4+ red candles)")
                    
                    # Check for strong follow-through
                    total_move = abs(recent[-1]["close"] - recent[0]["open"])
                    pip_size = self._get_pip_size(trade_record['symbol'])
                    move_pips = total_move / pip_size
                    
                    if move_pips > 15:
                        reasons.append(f"Strong directional move ({move_pips:.1f} pips)")
                
                if not reasons:
                    reasons.append("TP hit - setup played out as expected")
                
                analysis['tp_hit_reason'] = " | ".join(reasons)
                analysis['lessons_learned'] = "Good trade - setup criteria validated. Repeat this pattern."
            
            else:
                # Partial exit or trailing stop win
                analysis['tp_hit_reason'] = "Partial profit / trailing stop capture"
                analysis['lessons_learned'] = "Good risk management - locked in profits early"
        
        return analysis
    
    # ── Get Adaptive Confidence ──────────────────────────────────────────────
    def get_adaptive_confidence(self, setup_type: str) -> float:
        """
        Calculate REAL confidence based on actual performance of last 10-50 trades.
        Updates every 10 trades.
        """
        confidence = self.memory.get_setup_confidence(setup_type)
        
        # Get stop hit patterns to adjust confidence
        stop_patterns = self.memory.get_stop_hit_analysis(setup_type)
        
        # If one stop pattern accounts for > 50%, reduce confidence
        if stop_patterns:
            top_pattern = stop_patterns[0]
            if top_pattern['percentage'] > 50:
                penalty = 10  # 10% confidence penalty
                logger.warning(f"⚠️  {setup_type}: {top_pattern['reason']} accounts for "
                               f"{top_pattern['percentage']:.0f}% of losses - reducing confidence")
                confidence = max(confidence - penalty, 0)
        
        return confidence
    
    # ── Check if Should Disable Setup ────────────────────────────────────────
    def should_disable_setup(self, setup_type: str) -> bool:
        """
        Disable setup if:
          • Has 50+ trades AND
          • Confidence < 60%
        """
        exec_cfg = self.config.get("execution", {}) if isinstance(self.config.get("execution", {}), dict) else {}
        forced = exec_cfg.get("force_enable_setups", [])
        if not isinstance(forced, list):
            forced = []
        forced_set = {str(x).upper().strip() for x in forced if str(x).strip()}
        if str(setup_type or "").upper() in forced_set:
            logger.info(f"SETUP_OVERRIDE_FORCE_ENABLED: {setup_type}")
            return False
        return not self.memory.is_setup_enabled(setup_type)
    
    # ── Generate Performance Report ──────────────────────────────────────────
    def generate_performance_report(self) -> str:
        """
        Generate a detailed performance report for all setups.
        Shows which setups work and which need adjustment.
        """
        setups = self.memory.get_all_setup_performance()
        
        if not setups:
            return "No performance data yet - bot is learning..."
        
        report = ["=" * 60]
        report.append("TRADING BRAIN - PERFORMANCE ANALYSIS")
        report.append("=" * 60)
        report.append("")
        
        for setup in setups:
            status = "✅ ENABLED" if setup['enabled'] else "❌ DISABLED"
            report.append(f"{setup['setup']:15} | {status:12} | Trades: {setup['trades']:3} | "
                          f"WR: {setup['win_rate']:5.1f}% | Conf: {setup['confidence']:5.1f}%")
            
            # Get stop patterns for this setup
            patterns = self.memory.get_stop_hit_analysis(setup['setup'])
            if patterns:
                report.append(f"  └─ Top stop reasons:")
                for p in patterns[:3]:
                    report.append(f"     • {p['reason']:40} ({p['percentage']:.0f}%)")
            report.append("")
        
        report.append("=" * 60)
        report.append(f"Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")
        report.append("=" * 60)
        
        return "\n".join(report)
    
    def _get_pip_size(self, symbol: str) -> float:
        """Get pip size for PnL calculations. JPY=0.01, indices=1.0, gold=0.1, FX=0.0001."""
        if "JPY" in symbol:
            return 0.01
        if symbol in ("US30", "NAS100", "SPX500"):
            return 1.0
        if "XAU" in symbol:
            return 0.1
        return 0.0001
