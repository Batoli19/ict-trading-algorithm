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
from datetime import datetime, timezone
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
    order_ticket: Optional[int] = None
    deal_ticket: Optional[int] = None
    position_id: Optional[int] = None
    
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
            order_ticket INTEGER,
            deal_ticket INTEGER,
            position_id INTEGER,
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
        self._ensure_columns()
        logger.info(f"✅  Memory database initialized: {self.db_path}")

    def _ensure_columns(self):
        """Backfill newer identifier columns for existing databases."""
        cursor = self.conn.cursor()
        cursor.execute("PRAGMA table_info(trades)")
        cols = {row[1] for row in cursor.fetchall()}

        if "order_ticket" not in cols:
            cursor.execute("ALTER TABLE trades ADD COLUMN order_ticket INTEGER")
        if "deal_ticket" not in cols:
            cursor.execute("ALTER TABLE trades ADD COLUMN deal_ticket INTEGER")
        if "position_id" not in cols:
            cursor.execute("ALTER TABLE trades ADD COLUMN position_id INTEGER")

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_position_id ON trades(position_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_order_ticket ON trades(order_ticket)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_deal_ticket ON trades(deal_ticket)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_outcome_exit_time ON trades(outcome, exit_time)")
        self.conn.commit()
    
    # ── Record Trade Entry ────────────────────────────────────────────────────
    def record_entry(self, trade: TradeMemory):
        """Record a new trade entry with full context"""
        cursor = self.conn.cursor()
        
        cursor.execute("""
        INSERT INTO trades (
            ticket, order_ticket, deal_ticket, position_id,
            symbol, direction, setup_type,
            entry_price, sl_price, tp_price, lot_size,
            htf_bias, kill_zone, spread_pips,
            reason, conditions_met, expected_outcome, confidence_input,
            entry_time
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            trade.ticket, trade.order_ticket, trade.deal_ticket, trade.position_id,
            trade.symbol, trade.direction, trade.setup_type,
            trade.entry_price, trade.sl_price, trade.tp_price, trade.lot_size,
            trade.htf_bias, trade.kill_zone, trade.spread_pips,
            trade.reason, "|".join(trade.conditions_met), trade.expected_outcome,
            trade.confidence_input, trade.entry_time
        ))
        
        self.conn.commit()
        logger.info(f"📝  Recorded entry: {trade.setup_type} {trade.symbol} #{trade.ticket}")

    def get_open_trades(self, limit: Optional[int] = None) -> List[Dict]:
        """Return currently open DB trades."""
        cursor = self.conn.cursor()
        q = """
        SELECT id, ticket, symbol, direction, setup_type, entry_price, sl_price, tp_price,
               position_id, order_ticket, deal_ticket, entry_time
        FROM trades
        WHERE outcome IS NULL OR outcome = '' OR outcome = 'OPEN'
        ORDER BY id DESC
        """
        params = ()
        if limit is not None:
            q += " LIMIT ?"
            params = (int(limit),)
        cursor.execute(q, params)
        rows = []
        for row in cursor.fetchall():
            rows.append({
                "id": row[0],
                "ticket": row[1],
                "symbol": row[2],
                "direction": row[3],
                "setup_type": row[4],
                "entry_price": row[5],
                "sl_price": row[6],
                "tp_price": row[7],
                "position_id": row[8],
                "order_ticket": row[9],
                "deal_ticket": row[10],
                "entry_time": str(row[11]) if row[11] is not None else None,
            })
        return rows
    
    # ── Record Trade Exit ─────────────────────────────────────────────────────
    def record_exit(self,
                    ticket: Optional[int] = None,
                    order_ticket: Optional[int] = None,
                    deal_ticket: Optional[int] = None,
                    position_id: Optional[int] = None,
                    exit_price: float = 0.0,
                    pnl: float = 0.0,
                    exit_time: Optional[datetime] = None,
                    stop_hit_reason: Optional[str] = None,
                    tp_hit_reason: Optional[str] = None,
                    lessons: Optional[str] = None) -> bool:
        """Record trade exit and analysis. Returns True when a row was updated."""
        cursor = self.conn.cursor()

        if pnl > 0:
            outcome = "WIN"
        elif pnl < 0:
            outcome = "LOSS"
        else:
            outcome = "BREAKEVEN"

        trade_row = self._resolve_open_trade_for_exit(
            position_id=position_id,
            order_ticket=order_ticket,
            ticket=ticket,
            deal_ticket=deal_ticket,
        )
        if not trade_row:
            logger.warning(
                f"No open trade matched for exit "
                f"(position_id={position_id}, order={order_ticket}, ticket={ticket}, deal={deal_ticket})"
            )
            return False

        trade_id = trade_row["id"]
        trade_ticket = trade_row["ticket"]
        resolved_exit_time = exit_time or datetime.utcnow()

        cursor.execute("""
        UPDATE trades
        SET exit_price = ?, exit_time = ?, pnl = ?, outcome = ?,
            stop_hit_reason = ?, tp_hit_reason = ?, lessons_learned = ?,
            deal_ticket = COALESCE(?, deal_ticket),
            order_ticket = COALESCE(?, order_ticket),
            position_id = COALESCE(?, position_id)
        WHERE id = ? AND (outcome IS NULL OR outcome = '' OR outcome = 'OPEN')
        """, (
            exit_price, resolved_exit_time, pnl, outcome,
            stop_hit_reason, tp_hit_reason, lessons,
            deal_ticket, order_ticket, position_id,
            trade_id
        ))
        rows_affected = cursor.rowcount
        self.conn.commit()
        if rows_affected == 0:
            logger.warning(f"Exit update skipped; trade already closed id={trade_id} ticket={trade_ticket}")
            return False

        setup_type = trade_row["setup_type"]
        if setup_type:
            self._update_setup_performance(setup_type)
            if outcome == "LOSS" and stop_hit_reason:
                self._record_stop_pattern(setup_type, stop_hit_reason)

        logger.info(
            f"📝  Recorded exit: #{trade_ticket} | {outcome} | P&L: {pnl:+.2f} "
            f"| pos={position_id} order={order_ticket} deal={deal_ticket}"
        )
        return True

    def update_exit_analysis(self,
                             trade_id: Optional[int] = None,
                             ticket: Optional[int] = None,
                             stop_hit_reason: Optional[str] = None,
                             tp_hit_reason: Optional[str] = None,
                             lessons: Optional[str] = None) -> bool:
        cursor = self.conn.cursor()
        if trade_id is not None:
            cursor.execute("""
            UPDATE trades
            SET stop_hit_reason = ?, tp_hit_reason = ?, lessons_learned = ?
            WHERE id = ?
            """, (stop_hit_reason, tp_hit_reason, lessons, int(trade_id)))
        elif ticket is not None:
            cursor.execute("""
            UPDATE trades
            SET stop_hit_reason = ?, tp_hit_reason = ?, lessons_learned = ?
            WHERE ticket = ?
            """, (stop_hit_reason, tp_hit_reason, lessons, int(ticket)))
        else:
            return False
        updated = cursor.rowcount > 0
        self.conn.commit()
        return updated
    
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
        SELECT ticket, order_ticket, deal_ticket, position_id,
               symbol, setup_type, direction, outcome, pnl,
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
                'order_ticket': row[1],
                'deal_ticket': row[2],
                'position_id': row[3],
                'symbol': row[4],
                'setup': row[5],
                'direction': row[6],
                'outcome': row[7],
                'pnl': row[8],
                'reason': row[9],
                'expected': row[10],
                'stop_reason': row[11],
                'lessons': row[12],
                'time': row[13]
            })
        
        return trades

    def _parse_db_datetime(self, value) -> Optional[datetime]:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value
        text = str(value).strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            pass
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(text, fmt)
            except Exception:
                continue
        return None

    def _resolve_open_trade_for_exit(self,
                                     position_id: Optional[int] = None,
                                     order_ticket: Optional[int] = None,
                                     ticket: Optional[int] = None,
                                     deal_ticket: Optional[int] = None) -> Optional[Dict]:
        cursor = self.conn.cursor()

        lookup_chain = [
            ("position_id", position_id),
            ("ticket", ticket),
            ("order_ticket", order_ticket),
            ("deal_ticket", deal_ticket),
        ]
        for col, value in lookup_chain:
            if value is None:
                continue
            cursor.execute(f"""
            SELECT id, ticket, symbol, direction, setup_type,
                   entry_price, sl_price, tp_price,
                   position_id, order_ticket, deal_ticket
            FROM trades
            WHERE (outcome IS NULL OR outcome = '' OR outcome = 'OPEN') AND {col} = ?
            ORDER BY id DESC
            LIMIT 1
            """, (value,))
            row = cursor.fetchone()
            if row:
                return {
                    "id": row[0],
                    "ticket": row[1],
                    "symbol": row[2],
                    "direction": row[3],
                    "setup_type": row[4],
                    "entry_price": row[5],
                    "sl_price": row[6],
                    "tp_price": row[7],
                    "position_id": row[8],
                    "order_ticket": row[9],
                    "deal_ticket": row[10],
                    "matched_by": col,
                }
        if position_id is not None:
            cursor.execute("""
            SELECT id, ticket, symbol, direction, setup_type,
                   entry_price, sl_price, tp_price,
                   position_id, order_ticket, deal_ticket
            FROM trades
            WHERE (outcome IS NULL OR outcome = '' OR outcome = 'OPEN') AND ticket = ?
            ORDER BY id DESC
            LIMIT 1
            """, (position_id,))
            row = cursor.fetchone()
            if row:
                return {
                    "id": row[0],
                    "ticket": row[1],
                    "symbol": row[2],
                    "direction": row[3],
                    "setup_type": row[4],
                    "entry_price": row[5],
                    "sl_price": row[6],
                    "tp_price": row[7],
                    "position_id": row[8],
                    "order_ticket": row[9],
                    "deal_ticket": row[10],
                    "matched_by": "ticket_from_position_id",
                }
        return None

    def find_open_trade_for_exit(self,
                                 position_id: Optional[int] = None,
                                 order_ticket: Optional[int] = None,
                                 ticket: Optional[int] = None,
                                 deal_ticket: Optional[int] = None) -> Optional[Dict]:
        return self._resolve_open_trade_for_exit(
            position_id=position_id,
            order_ticket=order_ticket,
            ticket=ticket,
            deal_ticket=deal_ticket,
        )

    def get_closed_trades_between(self, start_utc: datetime, end_utc: datetime) -> List[Dict]:
        cursor = self.conn.cursor()
        cursor.execute("""
        SELECT ticket, order_ticket, deal_ticket, position_id, symbol, setup_type,
               outcome, pnl, entry_time, exit_time, exit_price
        FROM trades
        WHERE outcome IS NOT NULL AND exit_time IS NOT NULL
        ORDER BY exit_time DESC
        """)

        rows = []
        for row in cursor.fetchall():
            exit_time = self._parse_db_datetime(row[9])
            if not exit_time:
                continue
            if not (start_utc <= exit_time < end_utc):
                continue
            rows.append({
                "ticket": row[0],
                "order_ticket": row[1],
                "deal_ticket": row[2],
                "position_id": row[3],
                "symbol": row[4],
                "setup_type": row[5],
                "outcome": row[6],
                "pnl": float(row[7] or 0.0),
                "entry_time": str(row[8]) if row[8] is not None else None,
                "exit_time": exit_time.isoformat(),
                "exit_price": float(row[10] or 0.0),
            })
        return rows

    def count_trades_for_symbol_between(self, symbol: str, start_utc: datetime, end_utc: datetime) -> int:
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT COUNT(*) FROM trades
            WHERE symbol = ?
              AND entry_time >= ?
              AND entry_time < ?
            """,
            (symbol, start_utc.isoformat(), end_utc.isoformat())
        )
        row = cur.fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def _utc_day_window(self):
        now = datetime.now(timezone.utc)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0).replace(tzinfo=None)
        end = start.replace(hour=23, minute=59, second=59, microsecond=999999)
        return start, end

    def count_trades_today_total(self) -> int:
        start, end = self._utc_day_window()
        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM trades WHERE entry_time BETWEEN ? AND ?", (start, end))
        return int(cur.fetchone()[0] or 0)

    def count_trades_today_symbol(self, symbol: str) -> int:
        start, end = self._utc_day_window()
        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM trades WHERE symbol=? AND entry_time BETWEEN ? AND ?", (symbol, start, end))
        return int(cur.fetchone()[0] or 0)

    def count_trades_today_symbol_kz(self, symbol: str, kill_zone: str) -> int:
        start, end = self._utc_day_window()
        cur = self.conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM trades WHERE symbol=? AND kill_zone=? AND entry_time BETWEEN ? AND ?",
            (symbol, kill_zone, start, end),
        )
        return int(cur.fetchone()[0] or 0)

    def get_daily_summary(self, start_utc: datetime, end_utc: datetime) -> Dict:
        trades = self.get_closed_trades_between(start_utc, end_utc)
        pnls = [float(t["pnl"]) for t in trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]

        trades_count = len(trades)
        wins_count = len(wins)
        losses_count = len(losses)
        daily_pnl = sum(pnls)
        win_rate = (wins_count / trades_count * 100.0) if trades_count else 0.0
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)
        avg_pnl = (daily_pnl / trades_count) if trades_count else 0.0

        return {
            "trades_today_count": trades_count,
            "wins_today_count": wins_count,
            "losses_today_count": losses_count,
            "daily_pnl": round(daily_pnl, 2),
            "win_rate": round(win_rate, 2),
            "profit_factor": round(profit_factor, 4) if profit_factor != float("inf") else "inf",
            "avg_pnl": round(avg_pnl, 2),
        }

    def get_trade_counts(self) -> Dict:
        cursor = self.conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM trades")
        total = int(cursor.fetchone()[0] or 0)
        cursor.execute("SELECT COUNT(*) FROM trades WHERE outcome IS NOT NULL")
        closed = int(cursor.fetchone()[0] or 0)
        return {"total_trades": total, "closed_trades": closed}

    def get_last_trades_raw(self, limit: int = 5) -> List[Dict]:
        cursor = self.conn.cursor()
        cursor.execute("""
        SELECT id, ticket, order_ticket, deal_ticket, position_id, symbol, setup_type,
               outcome, pnl, entry_time, exit_time
        FROM trades
        ORDER BY id DESC
        LIMIT ?
        """, (limit,))
        items = []
        for row in cursor.fetchall():
            items.append({
                "id": row[0],
                "ticket": row[1],
                "order_ticket": row[2],
                "deal_ticket": row[3],
                "position_id": row[4],
                "symbol": row[5],
                "setup_type": row[6],
                "outcome": row[7],
                "pnl": row[8],
                "entry_time": str(row[9]) if row[9] is not None else None,
                "exit_time": str(row[10]) if row[10] is not None else None,
            })
        return items
    
    def close(self):
        """Close database connection"""
        if self.conn:
            self.conn.close()
