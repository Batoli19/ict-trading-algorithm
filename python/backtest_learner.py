"""
Backtest Learner — Adaptive Learning from Historical Losses
═══════════════════════════════════════════════════════════
Analyzes losing trades from a backtest run to discover repeating patterns,
generates avoidance rules, and can be used in a second pass to skip trades
that match those bad patterns.

Two-pass architecture:
    Pass 1 — Run & Record:
        Normal backtest runs, all trades are collected.
        This module then analyzes every losing trade to find patterns.

    Pass 2 — Learn & Re-run:
        Rules generated in Pass 1 are loaded into the backtester.
        Before each trade, the learner checks: "does this look like a
        pattern that lost money before?" If yes → skip.

Pattern detection looks at combinations of:
    - Setup type (FVG, ORDER_BLOCK, STOP_HUNT, etc.)
    - Direction (BUY/SELL)
    - Kill zone (LONDON_OPEN, NY_OPEN, LONDON_CLOSE, DEAD_ZONE)
    - HTF bias alignment (is direction aligned with H4 trend?)
    - Symbol

A rule is created when a specific combination loses more than the
threshold (default: 65% loss rate with minimum 5 samples).

Usage:
    from backtest_learner import BacktestLearner
    
    # After Pass 1
    learner = BacktestLearner(loss_threshold=0.65, min_samples=5)
    rules = learner.analyze_losses(trades)
    learner.print_summary()
    
    # Pass 2 — re-run with rules active
    engine = BacktestEngine(config, learner=learner)
    improved_trades = engine.run()
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from collections import defaultdict

logger = logging.getLogger("LEARNER")


# ═══════════════════════════════════════════════════════════════════════════
# AvoidanceRule — a learned pattern to avoid
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class AvoidanceRule:
    """
    A rule learned from historical losses.
    
    Example rule:
        "ORDER_BLOCK + SELL + LONDON_CLOSE + HTF_BULLISH → 80% loss rate"
    
    When the learner sees a new signal matching this pattern, it says "skip".
    """
    rule_id: str                  # Unique identifier for this rule
    setup_type: str               # e.g. "ORDER_BLOCK"
    direction: str                # "BUY" or "SELL"
    killzone: Optional[str]       # e.g. "LONDON_CLOSE" or None (any KZ)
    htf_bias: Optional[str]       # e.g. "BEARISH" or None (any bias)
    symbol: Optional[str]         # e.g. "EURUSD" or None (any symbol)
    
    # Stats from backtest analysis
    total_trades: int = 0         # How many trades matched this pattern
    losing_trades: int = 0        # How many of those were losses
    loss_rate: float = 0.0        # losing_trades / total_trades
    avg_loss_pips: float = 0.0    # Average loss in pips for losing trades
    total_pnl_pips: float = 0.0   # Total PnL of all trades matching this pattern
    
    @property
    def reason(self) -> str:
        """Human-readable description of why this rule blocks trades."""
        parts = [self.setup_type, self.direction]
        if self.killzone:
            parts.append(f"in {self.killzone}")
        if self.htf_bias:
            parts.append(f"HTF={self.htf_bias}")
        if self.symbol:
            parts.append(f"on {self.symbol}")
        return (
            f"{' + '.join(parts)} → "
            f"{self.loss_rate:.0%} loss rate "
            f"({self.losing_trades}/{self.total_trades} trades, "
            f"{self.total_pnl_pips:+.1f} pips)"
        )


# ═══════════════════════════════════════════════════════════════════════════
# BacktestLearner — analyzes losses and generates avoidance rules
# ═══════════════════════════════════════════════════════════════════════════

class BacktestLearner:
    """
    Learns from backtest losses and generates avoidance rules.
    
    The learner groups trades by various condition combinations and finds
    groups where the loss rate exceeds the threshold. These become rules
    that can block future trades matching the same pattern.
    """
    
    def __init__(
        self,
        loss_threshold: float = 0.65,
        min_samples: int = 5,
        min_pnl_loss: float = -20.0,
    ):
        """
        Args:
            loss_threshold: Minimum loss rate to create a rule (default: 65%)
            min_samples:    Minimum trades needed to create a rule (default: 5)
            min_pnl_loss:   Minimum total PnL (negative) to create a rule (default: -20 pips)
        """
        self.loss_threshold = loss_threshold
        self.min_samples = min_samples
        self.min_pnl_loss = min_pnl_loss
        self.rules: List[AvoidanceRule] = []
        
        # Stats tracking
        self.total_losses_analyzed = 0
        self.total_trades_analyzed = 0
        self.skipped_count = 0          # How many signals were skipped in Pass 2
        self.skip_log: List[dict] = []  # Log of skipped signals for audit
    
    def analyze_losses(self, trades: list) -> List[AvoidanceRule]:
        """
        Analyze completed backtest trades and generate avoidance rules.
        
        Groups trades by multiple condition combinations and finds losing
        patterns. Returns the list of generated rules.
        
        Args:
            trades: List of SimulatedTrade objects from a backtest run
            
        Returns:
            List of AvoidanceRule objects (also stored in self.rules)
        """
        # Only analyze closed trades (not END_OF_DATA)
        closed = [
            t for t in trades
            if t.exit_reason in ("SL_HIT", "TP_HIT", "TRAILED_SL", "BREAKEVEN")
        ]
        
        self.total_trades_analyzed = len(closed)
        self.total_losses_analyzed = sum(1 for t in closed if not t.is_winner)
        
        if not closed:
            logger.warning("No closed trades to analyze")
            return []
        
        logger.info(f"Analyzing {len(closed)} trades ({self.total_losses_analyzed} losses)")
        
        # ─── Group trades by different condition combinations ──────────
        # We check multiple "dimension" combinations to find bad patterns.
        # Each combination is a tuple of field names to group by.
        grouping_dimensions = [
            # Level 1: Single dimension (broad patterns)
            ("setup_type",),
            ("setup_type", "direction"),
            ("setup_type", "killzone"),
            
            # Level 2: Two dimensions (more specific)
            ("setup_type", "direction", "killzone"),
            ("setup_type", "direction", "htf_bias"),
            
            # Level 3: Three dimensions (very specific)
            ("setup_type", "direction", "killzone", "htf_bias"),
            
            # Symbol-specific
            ("symbol", "setup_type"),
            ("symbol", "setup_type", "direction"),
        ]
        
        all_rules = []
        seen_keys = set()  # Avoid duplicate rules
        
        for dims in grouping_dimensions:
            groups = self._group_trades(closed, dims)
            
            for group_key_tuple, group_trades in groups.items():
                # Convert the frozen tuple key back to a dict for field access
                group_key = dict(group_key_tuple)
                total = len(group_trades)
                
                # Skip if not enough samples
                if total < self.min_samples:
                    continue
                
                losers = sum(1 for t in group_trades if not t.is_winner)
                loss_rate = losers / total
                total_pnl = sum(t.pnl_pips for t in group_trades)
                
                # Skip if loss rate is below threshold
                if loss_rate < self.loss_threshold:
                    continue
                
                # Skip if total PnL isn't bad enough
                if total_pnl > self.min_pnl_loss:
                    continue
                
                # Create a unique key to avoid duplicate rules
                if group_key_tuple in seen_keys:
                    continue
                seen_keys.add(group_key_tuple)
                
                # Calculate average loss for losing trades
                losing_pnls = [t.pnl_pips for t in group_trades if not t.is_winner]
                avg_loss = sum(losing_pnls) / len(losing_pnls) if losing_pnls else 0.0
                
                # Build the avoidance rule
                rule = AvoidanceRule(
                    rule_id=f"R{len(all_rules)+1:03d}",
                    setup_type=group_key.get("setup_type", "*"),
                    direction=group_key.get("direction", "*"),
                    killzone=group_key.get("killzone"),
                    htf_bias=group_key.get("htf_bias"),
                    symbol=group_key.get("symbol"),
                    total_trades=total,
                    losing_trades=losers,
                    loss_rate=loss_rate,
                    avg_loss_pips=avg_loss,
                    total_pnl_pips=total_pnl,
                )
                
                all_rules.append(rule)
                logger.info(f"  RULE {rule.rule_id}: {rule.reason}")
        
        # Sort by impact (total PnL loss, worst first)
        all_rules.sort(key=lambda r: r.total_pnl_pips)
        
        # Remove redundant rules: if a broad rule covers a narrow one,
        # keep only the narrow (more specific) rule to avoid over-blocking
        self.rules = self._deduplicate_rules(all_rules)
        
        logger.info(f"Generated {len(self.rules)} avoidance rules from {len(closed)} trades")
        return self.rules
    
    def should_skip(self, signal, conditions: dict) -> Tuple[bool, str]:
        """
        Check if a signal should be skipped based on learned rules.
        
        Called by BacktestEngine during Pass 2 before creating a trade.
        
        Args:
            signal:     The Signal object from ICTStrategy
            conditions: Dict with keys: symbol, setup_type, direction,
                       killzone, htf_bias, confidence
                       
        Returns:
            (should_skip: bool, reason: str)
        """
        if not self.rules:
            return False, ""
        
        setup = str(conditions.get("setup_type", "")).upper()
        direction = str(conditions.get("direction", "")).upper()
        killzone = str(conditions.get("killzone", "")).upper()
        htf_bias = str(conditions.get("htf_bias", "")).upper()
        symbol = str(conditions.get("symbol", "")).upper()
        
        for rule in self.rules:
            # Check if this rule matches the current signal
            if not self._rule_matches(rule, setup, direction, killzone, htf_bias, symbol):
                continue
            
            # Rule matches — skip this trade
            self.skipped_count += 1
            self.skip_log.append({
                "rule_id": rule.rule_id,
                "reason": rule.reason,
                **conditions,
            })
            
            return True, rule.reason
        
        return False, ""
    
    def _rule_matches(
        self,
        rule: AvoidanceRule,
        setup: str,
        direction: str,
        killzone: str,
        htf_bias: str,
        symbol: str,
    ) -> bool:
        """Check if a rule matches the given conditions."""
        # Setup type must match (unless rule is wildcard "*")
        if rule.setup_type != "*" and rule.setup_type.upper() != setup:
            return False
        
        # Direction must match (unless rule is wildcard "*")
        if rule.direction != "*" and rule.direction.upper() != direction:
            return False
        
        # Kill zone must match if specified in rule
        if rule.killzone and rule.killzone.upper() != killzone:
            return False
        
        # HTF bias must match if specified in rule
        if rule.htf_bias and rule.htf_bias.upper() != htf_bias:
            return False
        
        # Symbol must match if specified in rule
        if rule.symbol and rule.symbol.upper() != symbol:
            return False
        
        return True
    
    def _group_trades(self, trades: list, dimensions: tuple) -> dict:
        """
        Group trades by the specified dimensions.
        
        Args:
            trades:     List of SimulatedTrade objects
            dimensions: Tuple of field names to group by
                       (e.g. ("setup_type", "direction", "killzone"))
        
        Returns:
            Dict mapping group_key_dict -> list of trades in that group
        """
        groups = defaultdict(list)
        
        for trade in trades:
            # Build the group key from the specified dimensions
            key_parts = {}
            for dim in dimensions:
                if dim == "setup_type":
                    key_parts[dim] = trade.setup_type.upper()
                elif dim == "direction":
                    key_parts[dim] = trade.direction.upper()
                elif dim == "killzone":
                    key_parts[dim] = (trade.killzone or "NONE").upper()
                elif dim == "htf_bias":
                    key_parts[dim] = (trade.htf_bias or "NEUTRAL").upper()
                elif dim == "symbol":
                    key_parts[dim] = trade.symbol.upper()
            
            # Use a frozen tuple as the group key (hashable)
            frozen_key = tuple(sorted(key_parts.items()))
            groups[frozen_key].append(trade)
        
        # Keep frozen tuple keys (they're hashable and work as dict keys)
        return dict(groups)
    
    def _deduplicate_rules(self, rules: List[AvoidanceRule]) -> List[AvoidanceRule]:
        """
        Remove redundant rules where a broader rule covers a narrower one.
        
        Strategy: keep the MORE SPECIFIC rule (more conditions specified)
        because it's less likely to over-block good trades.
        
        Example:
            R001: ORDER_BLOCK + SELL → 70% loss (covers all OB SELLs)
            R002: ORDER_BLOCK + SELL + LONDON_CLOSE → 85% loss (specific KZ)
            
            Keep R002 (more specific), remove R001 (too broad, might block
            good OB SELLs during NY_OPEN which are profitable)
        """
        if len(rules) <= 1:
            return rules
        
        # Score each rule by specificity (more conditions = higher score)
        def specificity(rule: AvoidanceRule) -> int:
            score = 0
            if rule.setup_type != "*": score += 1
            if rule.direction != "*": score += 1
            if rule.killzone: score += 1
            if rule.htf_bias: score += 1
            if rule.symbol: score += 1
            return score
        
        # Sort by specificity (most specific first)
        rules.sort(key=lambda r: -specificity(r))
        
        kept = []
        for rule in rules:
            # Check if any already-kept rule is more specific and covers this one
            is_redundant = False
            for existing in kept:
                if self._rule_is_subset(rule, existing):
                    is_redundant = True
                    break
            
            if not is_redundant:
                kept.append(rule)
        
        return kept
    
    def _rule_is_subset(self, broad: AvoidanceRule, narrow: AvoidanceRule) -> bool:
        """Check if 'broad' is a less-specific version of 'narrow'."""
        # For broad to be a subset of narrow, every condition in broad
        # must match narrow, but narrow has MORE conditions.
        
        if broad.setup_type != "*" and narrow.setup_type != "*":
            if broad.setup_type.upper() != narrow.setup_type.upper():
                return False
        
        if broad.direction != "*" and narrow.direction != "*":
            if broad.direction.upper() != narrow.direction.upper():
                return False
        
        if broad.killzone and narrow.killzone:
            if broad.killzone.upper() != narrow.killzone.upper():
                return False
        
        # broad is a subset if narrow is more specific
        broad_spec = sum([
            broad.setup_type != "*",
            broad.direction != "*",
            bool(broad.killzone),
            bool(broad.htf_bias),
            bool(broad.symbol),
        ])
        narrow_spec = sum([
            narrow.setup_type != "*",
            narrow.direction != "*",
            bool(narrow.killzone),
            bool(narrow.htf_bias),
            bool(narrow.symbol),
        ])
        
        return narrow_spec > broad_spec
    
    def print_summary(self):
        """Print a human-readable summary of learned rules."""
        if not self.rules:
            print("\n  📭  No avoidance rules generated.")
            print(f"      (Analyzed {self.total_trades_analyzed} trades, "
                  f"{self.total_losses_analyzed} losses)")
            return
        
        print(f"\n{'═'*70}")
        print(f"  🧠  ADAPTIVE LEARNING SUMMARY")
        print(f"{'═'*70}")
        print(f"\n  Trades analyzed:  {self.total_trades_analyzed}")
        print(f"  Losses found:     {self.total_losses_analyzed}")
        print(f"  Rules generated:  {len(self.rules)}")
        print(f"  Loss threshold:   {self.loss_threshold:.0%}")
        print(f"  Min samples:      {self.min_samples}")
        
        print(f"\n  {'─'*66}")
        print(f"  AVOIDANCE RULES (sorted by impact)")
        print(f"  {'─'*66}")
        
        for rule in self.rules:
            icon = "🔴" if rule.loss_rate >= 0.80 else "🟡" if rule.loss_rate >= 0.70 else "🟠"
            print(f"  {icon} {rule.rule_id}: {rule.reason}")
        
        if self.skipped_count > 0:
            print(f"\n  {'─'*66}")
            print(f"  Pass 2 results: {self.skipped_count} trades blocked by rules")
        
        print(f"{'═'*70}\n")
    
    def get_rules_summary(self) -> dict:
        """Return a dict summary suitable for JSON export."""
        return {
            "total_trades_analyzed": self.total_trades_analyzed,
            "total_losses": self.total_losses_analyzed,
            "rules_count": len(self.rules),
            "loss_threshold": self.loss_threshold,
            "min_samples": self.min_samples,
            "skipped_in_pass2": self.skipped_count,
            "rules": [
                {
                    "id": r.rule_id,
                    "setup_type": r.setup_type,
                    "direction": r.direction,
                    "killzone": r.killzone,
                    "htf_bias": r.htf_bias,
                    "symbol": r.symbol,
                    "total_trades": r.total_trades,
                    "losing_trades": r.losing_trades,
                    "loss_rate": round(r.loss_rate, 3),
                    "total_pnl_pips": round(r.total_pnl_pips, 1),
                    "reason": r.reason,
                }
                for r in self.rules
            ],
        }
