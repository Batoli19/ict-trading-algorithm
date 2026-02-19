"""
Trading Memory System
─────────────────────
SQLite database that records:
  • Every trade taken with full context
  • Why the trade was taken (reasoning, conditions seen)
  • Expected outcome vs actual outcome
  • Why losses happened (stop hit location, market structure)
  • Setup-specific performance metrics
  
This is the bot's long-term memory.
"""

import sqlite3
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict
from dataclasses import dataclass

logger = logging.getLogger("MEMORY")


@dataclass
class TradeMemory:
    """Complete record of a trade with reasoning"""
    # Entry details
    ticket: int
    symbol: str
    direction: str  # BUY/SELL
    setup_type: str  # FVG, SNIPER, etc.
    entry_price: float
    sl_price: float
    tp_price: float
    lot_size: float
    
    # Context (why we took it)
    htf_bias: str  # BULLISH/BEARISH/NEUTRAL
    kill_zone: str  # LONDON_OPEN, NY_OPEN, etc.
    spread_pips: float
    
    # Reasoning (what we saw)
    reason: str  # Full explanation
    conditions_met: List[str]  # e.g., ["FVG identified", "Price in discount", "Bullish engulfing"]
    expected_outcome: str  # What we expect to happen
    confidence_input: float  # The confidence we assigned (pre-calculation)
    
    # Execution
    entry_time: datetime
    
    # Outcome (filled after trade closes)
    exit_price: Optional[float] = None
    exit_time: Optional[datetime] = None
    pnl: Optional[float] = None
    outcome: Optional[str] = None  # WIN, LOSS, BREAKEVEN
    
    # Analysis (why it won/lost)
    stop_hit_reason: Optional[str] = None  # e.g., "Fakeout wick", "News spike", "Structure broke"
    tp_hit_reason: Optional[str] = None  # e.g., "Clean move", "Momentum follow-through"
    lessons_learned: Optional[str] = None  # What we learned from this trade


class TradingMemoryDB:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.conn = None
        self._init_database()
    
    def _init_database(self):
        """Create database tables if they don't exist"""
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        cursor = self.conn.cursor()
        
        # Main trades table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket INTEGER UNIQUE,
            symbol TEXT,
            direction TEXT,
            setup_type TEXT,
            entry_price REAL,
            sl_price REAL,
            tp_price REAL,
            lot_size REAL,
            
            htf_bias TEXT,
            kill_zone TEXT,
            spread_pips REAL,
            
            reason TEXT,
            conditions_met TEXT,
            expected_outcome TEXT,
            confidence_input REAL,
            
            entry_time TIMESTAMP,
            exit_price REAL,
            exit_time TIMESTAMP,
            pnl REAL,
            outcome TEXT,
            
            stop_hit_reason TEXT,
            tp_hit_reason TEXT,
            lessons_learned TEXT,
            
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        
        # Setup performance table (aggregated stats per setup)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS setup_performance (
            setup_type TEXT PRIMARY KEY,
            total_trades INTEGER DEFAULT 0,
            wins INTEGER DEFAULT 0,
            losses INTEGER DEFAULT 0,
            breakevens INTEGER DEFAULT 0,
            win_rate REAL DEFAULT 0,
            avg_win REAL DEFAULT 0,
            avg_loss REAL DEFAULT 0,
            expectancy REAL DEFAULT 0,
            confidence_score REAL DEFAULT 50.0,
            enabled BOOLEAN DEFAULT 1,
            last_updated TIMESTAMP
        )
        """)
        
        # Stop hit patterns (where stops get hit most often)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS stop_patterns (
            setup_type TEXT,
            stop_hit_reason TEXT,
            occurrences INTEGER DEFAULT 1,
            percentage REAL,
            PRIMARY KEY (setup_type, stop_hit_reason)
        )
        """)
        
        self.conn.commit()
        logger.info(f"✅  Memory database initialized: {self.db_path}")
    
    # ── Record Trade Entry ────────────────────────────────────────────────────
    def record_entry(self, trade: TradeMemory):
        """Record a new trade entry with full context"""
        cursor = self.conn.cursor()
        
        cursor.execute("""
        INSERT INTO trades (
            ticket, symbol, direction, setup_type,
            entry_price, sl_price, tp_price, lot_size,
            htf_bias, kill_zone, spread_pips,
            reason, conditions_met, expected_outcome, confidence_input,
            entry_time
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            trade.ticket, trade.symbol, trade.direction, trade.setup_type,
            trade.entry_price, trade.sl_price, trade.tp_price, trade.lot_size,
            trade.htf_bias, trade.kill_zone, trade.spread_pips,
            trade.reason, "|".join(trade.conditions_met), trade.expected_outcome,
            trade.confidence_input, trade.entry_time
        ))
        
        self.conn.commit()
        logger.info(f"📝  Recorded entry: {trade.setup_type} {trade.symbol} #{trade.ticket}")
    
    # ── Record Trade Exit ─────────────────────────────────────────────────────
    def record_exit(self, ticket: int, exit_price: float, pnl: float,
                    stop_hit_reason: Optional[str] = None,
                    tp_hit_reason: Optional[str] = None,
                    lessons: Optional[str] = None):
        """Record trade exit and analysis"""
        cursor = self.conn.cursor()
        
        # Determine outcome
        if pnl > 0:
            outcome = "WIN"
        elif pnl < 0:
            outcome = "LOSS"
        else:
            outcome = "BREAKEVEN"
        
        cursor.execute("""
        UPDATE trades
        SET exit_price = ?, exit_time = ?, pnl = ?, outcome = ?,
            stop_hit_reason = ?, tp_hit_reason = ?, lessons_learned = ?
        WHERE ticket = ?
        """, (exit_price, datetime.utcnow(), pnl, outcome,
              stop_hit_reason, tp_hit_reason, lessons, ticket))
        
        self.conn.commit()
        
        # Update setup performance
        cursor.execute("SELECT setup_type FROM trades WHERE ticket = ?", (ticket,))
        result = cursor.fetchone()
        if result:
            setup_type = result[0]
            self._update_setup_performance(setup_type)
            
            # Record stop hit pattern if applicable
            if outcome == "LOSS" and stop_hit_reason:
                self._record_stop_pattern(setup_type, stop_hit_reason)
        
        logger.info(f"📝  Recorded exit: #{ticket} | {outcome} | P&L: {pnl:+.2f}")
    
    # ── Update Setup Performance ──────────────────────────────────────────────
    def _update_setup_performance(self, setup_type: str):
        """Recalculate performance metrics for a setup type"""
        cursor = self.conn.cursor()
        
        # Get all closed trades for this setup
        cursor.execute("""
        SELECT outcome, pnl FROM trades
        WHERE setup_type = ? AND outcome IS NOT NULL
        ORDER BY entry_time DESC
        """, (setup_type,))
        
        trades = cursor.fetchall()
        if not trades:
            return
        
        total = len(trades)
        wins = sum(1 for t in trades if t[0] == "WIN")
        losses = sum(1 for t in trades if t[0] == "LOSS")
        breakevens = sum(1 for t in trades if t[0] == "BREAKEVEN")
        
        win_rate = (wins / total * 100) if total > 0 else 0
        
        win_pnls = [t[1] for t in trades if t[0] == "WIN"]
        loss_pnls = [t[1] for t in trades if t[0] == "LOSS"]
        
        avg_win = sum(win_pnls) / len(win_pnls) if win_pnls else 0
        avg_loss = sum(loss_pnls) / len(loss_pnls) if loss_pnls else 0
        
        expectancy = (win_rate/100 * avg_win) + ((100-win_rate)/100 * avg_loss) if total > 0 else 0
        
        # Calculate confidence score based on last 50 trades (or all if < 50)
        recent_trades = trades[:50]
        recent_wins = sum(1 for t in recent_trades if t[0] == "WIN")
        confidence_score = (recent_wins / len(recent_trades) * 100) if recent_trades else 50.0
        
        # Disable setup if confidence < 60% and we have at least 50 trades
        enabled = True if total < 50 else confidence_score >= 60.0
        
        # Insert or update
        cursor.execute("""
        INSERT OR REPLACE INTO setup_performance (
            setup_type, total_trades, wins, losses, breakevens,
            win_rate, avg_win, avg_loss, expectancy, confidence_score,
            enabled, last_updated
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (setup_type, total, wins, losses, breakevens,
              win_rate, avg_win, avg_loss, expectancy, confidence_score,
              enabled, datetime.utcnow()))
        
        self.conn.commit()
        
        if not enabled:
            logger.warning(f"⚠️  Setup '{setup_type}' DISABLED: confidence {confidence_score:.1f}% < 60%")
        else:
            logger.info(f"📊  Setup '{setup_type}' updated: {total} trades, {win_rate:.1f}% WR, "
                        f"confidence: {confidence_score:.1f}%")
    
    # ── Record Stop Hit Pattern ───────────────────────────────────────────────
    def _record_stop_pattern(self, setup_type: str, reason: str):
        """Track why stops get hit for this setup"""
        cursor = self.conn.cursor()
        
        cursor.execute("""
        INSERT INTO stop_patterns (setup_type, stop_hit_reason, occurrences)
        VALUES (?, ?, 1)
        ON CONFLICT(setup_type, stop_hit_reason) 
        DO UPDATE SET occurrences = occurrences + 1
        """, (setup_type, reason))
        
        # Calculate percentage
        cursor.execute("""
        SELECT SUM(occurrences) FROM stop_patterns WHERE setup_type = ?
        """, (setup_type,))
        total = cursor.fetchone()[0] or 1
        
        cursor.execute("""
        UPDATE stop_patterns
        SET percentage = (occurrences * 100.0 / ?)
        WHERE setup_type = ?
        """, (total, setup_type))
        
        self.conn.commit()
    
    # ── Get Setup Confidence ──────────────────────────────────────────────────
    def get_setup_confidence(self, setup_type: str) -> float:
        """
        Get the real-time confidence score for a setup based on last 10-50 trades.
        Updates every 10 trades.
        """
        cursor = self.conn.cursor()
        
        # Get last 50 trades for this setup
        cursor.execute("""
        SELECT outcome FROM trades
        WHERE setup_type = ? AND outcome IS NOT NULL
        ORDER BY entry_time DESC
        LIMIT 50
        """, (setup_type,))
        
        trades = cursor.fetchall()
        
        if not trades:
            return 50.0  # Default for new setups
        
        total = len(trades)
        wins = sum(1 for t in trades if t[0] == "WIN")
        
        confidence = (wins / total * 100)
        
        # Update database every 10 trades
        if total % 10 == 0:
            self._update_setup_performance(setup_type)
        
        return confidence
    
    # ── Check if Setup Enabled ────────────────────────────────────────────────
    def is_setup_enabled(self, setup_type: str) -> bool:
        """Check if a setup type is enabled (>= 60% confidence with 50+ trades)"""
        cursor = self.conn.cursor()
        
        cursor.execute("""
        SELECT enabled, total_trades, confidence_score FROM setup_performance
        WHERE setup_type = ?
        """, (setup_type,))
        
        result = cursor.fetchone()
        
        if not result:
            return True  # New setup, allow it
        
        enabled, total, confidence = result
        
        if total < 50:
            return True  # Not enough data yet
        
        return bool(enabled)
    
    # ── Get Stop Hit Analysis ─────────────────────────────────────────────────
    def get_stop_hit_analysis(self, setup_type: str) -> List[Dict]:
        """Get the top reasons why stops get hit for this setup"""
        cursor = self.conn.cursor()
        
        cursor.execute("""
        SELECT stop_hit_reason, occurrences, percentage
        FROM stop_patterns
        WHERE setup_type = ?
        ORDER BY occurrences DESC
        LIMIT 5
        """, (setup_type,))
        
        patterns = []
        for row in cursor.fetchall():
            patterns.append({
                'reason': row[0],
                'occurrences': row[1],
                'percentage': row[2]
            })
        
        return patterns
    
    # ── Get Setup Performance ─────────────────────────────────────────────────
    def get_all_setup_performance(self) -> List[Dict]:
        """Get performance for all setups"""
        cursor = self.conn.cursor()
        
        cursor.execute("""
        SELECT setup_type, total_trades, win_rate, confidence_score, enabled
        FROM setup_performance
        ORDER BY confidence_score DESC
        """)
        
        setups = []
        for row in cursor.fetchall():
            setups.append({
                'setup': row[0],
                'trades': row[1],
                'win_rate': row[2],
                'confidence': row[3],
                'enabled': bool(row[4])
            })
        
        return setups
    
    # ── Get Recent Trades ─────────────────────────────────────────────────────
    def get_recent_trades(self, limit: int = 20) -> List[Dict]:
        """Get recent trades with full details"""
        cursor = self.conn.cursor()
        
        cursor.execute("""
        SELECT ticket, symbol, setup_type, direction, outcome, pnl,
               reason, expected_outcome, stop_hit_reason, lessons_learned,
               entry_time
        FROM trades
        WHERE outcome IS NOT NULL
        ORDER BY entry_time DESC
        LIMIT ?
        """, (limit,))
        
        trades = []
        for row in cursor.fetchall():
            trades.append({
                'ticket': row[0],
                'symbol': row[1],
                'setup': row[2],
                'direction': row[3],
                'outcome': row[4],
                'pnl': row[5],
                'reason': row[6],
                'expected': row[7],
                'stop_reason': row[8],
                'lessons': row[9],
                'time': row[10]
            })
        
        return trades
    
    def close(self):
        """Close database connection"""
        if self.conn:
            self.conn.close()
