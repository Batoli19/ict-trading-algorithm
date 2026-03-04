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

import re
import sqlite3
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict
from dataclasses import dataclass, field

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
    setup_class: str = ""  # CONTINUATION / REVERSAL
    validity_tags: List[str] = field(default_factory=list)
    
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
            setup_class TEXT,
            validity_tags TEXT,
            
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

        cursor.execute(
            """
        CREATE TABLE IF NOT EXISTS learned_lessons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket INTEGER,
            symbol TEXT,
            expected_direction TEXT,
            actual_direction TEXT,
            entry_reasons_json TEXT,
            entry_setups_json TEXT,
            entry_confidence REAL,
            missed_opposing_signals_json TEXT,
            strongest_opposing_setup TEXT,
            opposing_confluence_count INTEGER,
            lesson_summary TEXT,
            created_at_utc TIMESTAMP,
            htf_bias TEXT,
            kill_zone TEXT,
            spread_pips REAL
        )
        """
        )

        cursor.execute(
            """
        CREATE TABLE IF NOT EXISTS adaptive_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at_utc TIMESTAMP,
            rule_type TEXT,
            affected_setup TEXT,
            check_for TEXT,
            check_direction TEXT,
            threshold REAL,
            description TEXT,
            example TEXT,
            active BOOLEAN DEFAULT 0,
            sample_size INTEGER DEFAULT 0,
            wins_blocked_est REAL DEFAULT 0,
            losses_prevented_est REAL DEFAULT 0,
            times_triggered INTEGER DEFAULT 0,
            trades_blocked INTEGER DEFAULT 0,
            false_positives INTEGER DEFAULT 0,
            last_triggered_utc TIMESTAMP,
            expires_at_utc TIMESTAMP,
            status TEXT DEFAULT 'CANDIDATE'
        )
        """
        )

        cursor.execute(
            """
        CREATE TABLE IF NOT EXISTS rule_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_id INTEGER,
            event_time_utc TIMESTAMP,
            symbol TEXT,
            setup_id TEXT,
            direction TEXT,
            decision TEXT,
            notes TEXT
        )
        """
        )
        
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
        if "setup_class" not in cols:
            cursor.execute("ALTER TABLE trades ADD COLUMN setup_class TEXT")
        if "validity_tags" not in cols:
            cursor.execute("ALTER TABLE trades ADD COLUMN validity_tags TEXT")

        self._ensure_table_columns(
            "learned_lessons",
            {
                "ticket": "INTEGER",
                "symbol": "TEXT",
                "expected_direction": "TEXT",
                "actual_direction": "TEXT",
                "entry_reasons_json": "TEXT",
                "entry_setups_json": "TEXT",
                "entry_confidence": "REAL",
                "missed_opposing_signals_json": "TEXT",
                "strongest_opposing_setup": "TEXT",
                "opposing_confluence_count": "INTEGER",
                "lesson_summary": "TEXT",
                "created_at_utc": "TIMESTAMP",
                "htf_bias": "TEXT",
                "kill_zone": "TEXT",
                "spread_pips": "REAL",
            },
        )
        self._ensure_table_columns(
            "adaptive_rules",
            {
                "created_at_utc": "TIMESTAMP",
                "rule_type": "TEXT",
                "affected_setup": "TEXT",
                "check_for": "TEXT",
                "check_direction": "TEXT",
                "threshold": "REAL",
                "description": "TEXT",
                "example": "TEXT",
                "active": "BOOLEAN DEFAULT 0",
                "sample_size": "INTEGER DEFAULT 0",
                "wins_blocked_est": "REAL DEFAULT 0",
                "losses_prevented_est": "REAL DEFAULT 0",
                "times_triggered": "INTEGER DEFAULT 0",
                "trades_blocked": "INTEGER DEFAULT 0",
                "false_positives": "INTEGER DEFAULT 0",
                "last_triggered_utc": "TIMESTAMP",
                "expires_at_utc": "TIMESTAMP",
                "status": "TEXT DEFAULT 'CANDIDATE'",
            },
        )
        self._ensure_table_columns(
            "rule_events",
            {
                "rule_id": "INTEGER",
                "event_time_utc": "TIMESTAMP",
                "symbol": "TEXT",
                "setup_id": "TEXT",
                "direction": "TEXT",
                "decision": "TEXT",
                "notes": "TEXT",
            },
        )

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_position_id ON trades(position_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_order_ticket ON trades(order_ticket)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_deal_ticket ON trades(deal_ticket)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_outcome_exit_time ON trades(outcome, exit_time)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_lessons_symbol_time ON learned_lessons(symbol, created_at_utc)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_rules_status_setup ON adaptive_rules(status, affected_setup)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_rule_events_rule_time ON rule_events(rule_id, event_time_utc)")
        self.conn.commit()

    def _ensure_table_columns(self, table_name: str, columns_with_types: Dict[str, str]):
        cursor = self.conn.cursor()
        cursor.execute(f"PRAGMA table_info({table_name})")
        existing = {row[1] for row in cursor.fetchall()}
        for col_name, col_type in columns_with_types.items():
            if col_name in existing:
                continue
            cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_type}")
    
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
            reason, conditions_met, expected_outcome, confidence_input, setup_class, validity_tags,
            entry_time
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            trade.ticket, trade.order_ticket, trade.deal_ticket, trade.position_id,
            trade.symbol, trade.direction, trade.setup_type,
            trade.entry_price, trade.sl_price, trade.tp_price, trade.lot_size,
            trade.htf_bias, trade.kill_zone, trade.spread_pips,
            trade.reason, "|".join(trade.conditions_met), trade.expected_outcome,
            trade.confidence_input, trade.setup_class, "|".join(trade.validity_tags or []), trade.entry_time
        ))
        
        self.conn.commit()
        logger.info(f"📝  Recorded entry: {trade.setup_type} {trade.symbol} #{trade.ticket}")

    def get_open_trades(self, limit: Optional[int] = None, include_pending: bool = False) -> List[Dict]:
        """Return currently open DB trades.

        By default, only returns filled/open positions (position_id IS NOT NULL).
        Set include_pending=True to include unfilled pending-order placeholders.
        """
        cursor = self.conn.cursor()
        q = """
        SELECT id, ticket, symbol, direction, setup_type, entry_price, sl_price, tp_price,
               position_id, order_ticket, deal_ticket, entry_time, reason, htf_bias, kill_zone, spread_pips
        FROM trades
        WHERE outcome IS NULL OR outcome = '' OR outcome = 'OPEN'
        ORDER BY id DESC
        """
        if not include_pending:
            q = """
            SELECT id, ticket, symbol, direction, setup_type, entry_price, sl_price, tp_price,
                   position_id, order_ticket, deal_ticket, entry_time, reason, htf_bias, kill_zone, spread_pips
            FROM trades
            WHERE (outcome IS NULL OR outcome = '' OR outcome = 'OPEN')
              AND position_id IS NOT NULL
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
                "reason": row[12],
                "htf_bias": row[13],
                "kill_zone": row[14],
                "spread_pips": row[15],
            })
        return rows

    def infer_setup_type_from_comment(self, comment: Optional[str]) -> str:
        text = str(comment or "").strip().upper()
        if not text:
            return "UNKNOWN"
        if text.startswith("ICT_"):
            text = text[4:]
        text = text.replace("_LIMIT", "").replace("_CLOSE", "").strip("_")
        parts = [p for p in re.split(r"[^A-Z0-9]+", text) if p]
        if len(parts) >= 2:
            joined2 = f"{parts[0]}_{parts[1]}"
            if joined2 in {"ORDER_BLOCK", "STOP_HUNT", "PIN_BAR"}:
                return joined2
        if parts:
            return parts[0]
        return "UNKNOWN"

    def ensure_open_trade_from_position(self, position: Dict) -> str:
        """Ensure a live MT5 position exists in DB as an open trade.

        Returns one of: 'exists', 'linked', 'inserted', 'invalid'
        """
        try:
            pos_ticket = int(position.get("ticket"))
        except Exception:
            return "invalid"
        if pos_ticket <= 0:
            return "invalid"

        symbol = str(position.get("symbol") or "").upper().strip()
        direction = str(position.get("type") or "").upper().strip()
        setup_type = self.infer_setup_type_from_comment(position.get("comment"))
        entry_price = float(position.get("open_price") or 0.0)
        sl = float(position.get("sl") or 0.0)
        tp = float(position.get("tp") or 0.0)
        lot = float(position.get("volume") or 0.0)
        open_time = position.get("open_time") or datetime.utcnow()

        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT id, position_id
            FROM trades
            WHERE (outcome IS NULL OR outcome = '' OR outcome = 'OPEN')
              AND (position_id = ? OR ticket = ? OR order_ticket = ?)
            ORDER BY id DESC
            LIMIT 1
            """,
            (pos_ticket, pos_ticket, pos_ticket),
        )
        row = cursor.fetchone()
        if row:
            trade_id = int(row[0])
            had_position_id = row[1] is not None
            cursor.execute(
                """
                UPDATE trades
                SET position_id = COALESCE(position_id, ?),
                    ticket = COALESCE(ticket, ?),
                    order_ticket = COALESCE(order_ticket, ?),
                    symbol = CASE WHEN symbol IS NULL OR symbol = '' THEN ? ELSE symbol END,
                    direction = CASE WHEN direction IS NULL OR direction = '' THEN ? ELSE direction END,
                    setup_type = CASE WHEN setup_type IS NULL OR setup_type = '' THEN ? ELSE setup_type END,
                    entry_price = CASE WHEN entry_price IS NULL OR entry_price = 0 THEN ? ELSE entry_price END,
                    sl_price = CASE WHEN sl_price IS NULL OR sl_price = 0 THEN ? ELSE sl_price END,
                    tp_price = CASE WHEN tp_price IS NULL OR tp_price = 0 THEN ? ELSE tp_price END,
                    lot_size = CASE WHEN lot_size IS NULL OR lot_size = 0 THEN ? ELSE lot_size END,
                    entry_time = COALESCE(entry_time, ?)
                WHERE id = ?
                """,
                (
                    pos_ticket,
                    pos_ticket,
                    pos_ticket,
                    symbol,
                    direction,
                    setup_type,
                    entry_price,
                    sl,
                    tp,
                    lot,
                    open_time,
                    trade_id,
                ),
            )
            self.conn.commit()
            if had_position_id:
                return "exists"
            logger.info("DB_OPEN_SYNC_LINKED ticket=%s symbol=%s direction=%s", pos_ticket, symbol, direction)
            return "linked"

        try:
            cursor.execute(
                """
                INSERT INTO trades (
                    ticket, order_ticket, deal_ticket, position_id,
                    symbol, direction, setup_type,
                    entry_price, sl_price, tp_price, lot_size,
                    htf_bias, kill_zone, spread_pips,
                    reason, conditions_met, expected_outcome, confidence_input, setup_class, validity_tags,
                    entry_time
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pos_ticket, pos_ticket, None, pos_ticket,
                    symbol, direction, setup_type,
                    entry_price, sl, tp, lot,
                    "UNKNOWN", "UNKNOWN", 0.0,
                    "Recovered live position from MT5 sync",
                    "LIVE_SYNC_RECOVERED",
                    "Recovered open position from MT5 reconciliation",
                    0.5, "UNKNOWN", "LIVE_SYNC_RECOVERED",
                    open_time,
                ),
            )
            self.conn.commit()
            logger.warning(
                "DB_OPEN_SYNC_INSERTED ticket=%s symbol=%s direction=%s setup=%s",
                pos_ticket,
                symbol,
                direction,
                setup_type,
            )
            return "inserted"
        except sqlite3.IntegrityError:
            logger.warning("DB_OPEN_SYNC_DUPLICATE ticket=%s symbol=%s", pos_ticket, symbol)
            return "exists"

    def ensure_entry_trade_from_deal(self, deal: Dict) -> str:
        """Ensure a DB trade row exists for an MT5 entry deal.

        Returns one of: 'exists', 'inserted', 'invalid'
        """
        entry_flag = deal.get("entry")
        if entry_flag not in (0,):
            return "invalid"

        try:
            position_id = int(deal.get("position_id") or 0)
            order_ticket = int(deal.get("order_ticket") or 0)
            deal_ticket = int(deal.get("deal_ticket") or deal.get("ticket") or 0)
        except Exception:
            return "invalid"
        if position_id <= 0 and order_ticket <= 0 and deal_ticket <= 0:
            return "invalid"

        symbol = str(deal.get("symbol") or "").upper().strip()
        direction = str(deal.get("type") or "").upper().strip()
        setup_type = self.infer_setup_type_from_comment(deal.get("comment"))
        entry_price = float(deal.get("price") or 0.0)
        volume = float(deal.get("volume") or 0.0)
        time_raw = self._parse_db_datetime(deal.get("time"))
        entry_time = time_raw or datetime.utcnow()
        canonical_ticket = position_id if position_id > 0 else (order_ticket if order_ticket > 0 else deal_ticket)

        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT id
            FROM trades
            WHERE (position_id = ? AND position_id IS NOT NULL)
               OR ticket = ?
               OR (order_ticket = ? AND order_ticket IS NOT NULL)
            ORDER BY id DESC
            LIMIT 1
            """,
            (position_id, canonical_ticket, order_ticket),
        )
        row = cursor.fetchone()
        if row:
            return "exists"

        try:
            cursor.execute(
                """
                INSERT INTO trades (
                    ticket, order_ticket, deal_ticket, position_id,
                    symbol, direction, setup_type,
                    entry_price, sl_price, tp_price, lot_size,
                    htf_bias, kill_zone, spread_pips,
                    reason, conditions_met, expected_outcome, confidence_input, setup_class, validity_tags,
                    entry_time
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    canonical_ticket,
                    order_ticket if order_ticket > 0 else canonical_ticket,
                    deal_ticket if deal_ticket > 0 else None,
                    position_id if position_id > 0 else canonical_ticket,
                    symbol,
                    direction,
                    setup_type,
                    entry_price,
                    0.0,
                    0.0,
                    volume,
                    "UNKNOWN",
                    "UNKNOWN",
                    0.0,
                    "Recovered entry from MT5 deal sync",
                    "DEAL_SYNC_RECOVERED",
                    "Recovered entry from MT5 deal reconciliation",
                    0.5,
                    "UNKNOWN",
                    "DEAL_SYNC_RECOVERED",
                    entry_time,
                ),
            )
            self.conn.commit()
            logger.warning(
                "DB_DEAL_SYNC_INSERTED ticket=%s pos=%s symbol=%s direction=%s setup=%s",
                canonical_ticket,
                position_id,
                symbol,
                direction,
                setup_type,
            )
            return "inserted"
        except sqlite3.IntegrityError:
            logger.warning("DB_DEAL_SYNC_DUPLICATE ticket=%s pos=%s", canonical_ticket, position_id)
            return "exists"
    
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
               reason, expected_outcome, stop_hit_reason, lessons_learned, setup_class, validity_tags,
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
                'setup_class': row[13],
                'validity_tags': row[14].split("|") if row[14] else [],
                'time': row[15]
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

    def get_overall_summary(self) -> Dict:
        """All-time closed-trade stats from DB (persistent across restarts)."""
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT
                COUNT(*) AS trades,
                SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) AS wins,
                SUM(CASE WHEN outcome='LOSS' THEN 1 ELSE 0 END) AS losses,
                SUM(COALESCE(pnl, 0)) AS total_pnl,
                AVG(CASE WHEN outcome='WIN' THEN pnl END) AS avg_win,
                AVG(CASE WHEN outcome='LOSS' THEN pnl END) AS avg_loss
            FROM trades
            WHERE outcome IS NOT NULL
            """
        )
        row = cursor.fetchone() or (0, 0, 0, 0.0, 0.0, 0.0)
        trades = int(row[0] or 0)
        wins = int(row[1] or 0)
        losses = int(row[2] or 0)
        total_pnl = float(row[3] or 0.0)
        avg_win = float(row[4] or 0.0)
        avg_loss = float(row[5] or 0.0)
        win_rate = (wins / trades * 100.0) if trades > 0 else 0.0
        expectancy = ((wins / trades) * avg_win + (losses / trades) * avg_loss) if trades > 0 else 0.0
        return {
            "trades": trades,
            "wins": wins,
            "losses": losses,
            "winrate": round(win_rate, 2),
            "win_rate": round(win_rate, 2),
            "total_pnl": round(total_pnl, 2),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "expectancy": round(expectancy, 2),
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

    def save_learned_lesson(self, lesson: Dict) -> int:
        cursor = self.conn.cursor()
        cursor.execute(
            """
            INSERT INTO learned_lessons (
                ticket, symbol, expected_direction, actual_direction,
                entry_reasons_json, entry_setups_json, entry_confidence,
                missed_opposing_signals_json, strongest_opposing_setup,
                opposing_confluence_count, lesson_summary,
                created_at_utc, htf_bias, kill_zone, spread_pips
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(lesson.get("ticket") or 0),
                str(lesson.get("symbol") or ""),
                str(lesson.get("expected_direction") or ""),
                str(lesson.get("actual_direction") or ""),
                str(lesson.get("entry_reasons_json") or "[]"),
                str(lesson.get("entry_setups_json") or "[]"),
                float(lesson.get("entry_confidence") or 0.0),
                str(lesson.get("missed_opposing_signals_json") or "[]"),
                str(lesson.get("strongest_opposing_setup") or ""),
                int(lesson.get("opposing_confluence_count") or 0),
                str(lesson.get("lesson_summary") or ""),
                lesson.get("created_at_utc") or datetime.utcnow(),
                str(lesson.get("htf_bias") or ""),
                str(lesson.get("kill_zone") or ""),
                float(lesson.get("spread_pips") or 0.0),
            ),
        )
        self.conn.commit()
        return int(cursor.lastrowid or 0)

    def save_adaptive_rule(self, rule: Dict) -> int:
        cursor = self.conn.cursor()
        rule_id = int(rule.get("id") or 0)
        payload = (
            rule.get("created_at_utc") or datetime.utcnow(),
            str(rule.get("rule_type") or ""),
            str(rule.get("affected_setup") or ""),
            str(rule.get("check_for") or ""),
            str(rule.get("check_direction") or ""),
            float(rule.get("threshold") or 0.0),
            str(rule.get("description") or ""),
            str(rule.get("example") or ""),
            1 if bool(rule.get("active", False)) else 0,
            int(rule.get("sample_size") or 0),
            float(rule.get("wins_blocked_est") or 0.0),
            float(rule.get("losses_prevented_est") or 0.0),
            int(rule.get("times_triggered") or 0),
            int(rule.get("trades_blocked") or 0),
            int(rule.get("false_positives") or 0),
            rule.get("last_triggered_utc"),
            rule.get("expires_at_utc"),
            str(rule.get("status") or "CANDIDATE"),
        )
        if rule_id > 0:
            cursor.execute(
                """
                UPDATE adaptive_rules
                SET created_at_utc=?, rule_type=?, affected_setup=?, check_for=?, check_direction=?, threshold=?,
                    description=?, example=?, active=?, sample_size=?, wins_blocked_est=?, losses_prevented_est=?,
                    times_triggered=?, trades_blocked=?, false_positives=?, last_triggered_utc=?, expires_at_utc=?,
                    status=?
                WHERE id=?
                """,
                (*payload, rule_id),
            )
            if int(cursor.rowcount or 0) > 0:
                self.conn.commit()
                return rule_id
            cursor.execute(
                """
                INSERT INTO adaptive_rules (
                    id, created_at_utc, rule_type, affected_setup, check_for, check_direction, threshold,
                    description, example, active, sample_size, wins_blocked_est, losses_prevented_est,
                    times_triggered, trades_blocked, false_positives, last_triggered_utc, expires_at_utc, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (rule_id, *payload),
            )
            self.conn.commit()
            return rule_id
        cursor.execute(
            """
            INSERT INTO adaptive_rules (
                created_at_utc, rule_type, affected_setup, check_for, check_direction, threshold,
                description, example, active, sample_size, wins_blocked_est, losses_prevented_est,
                times_triggered, trades_blocked, false_positives, last_triggered_utc, expires_at_utc, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            payload,
        )
        self.conn.commit()
        return int(cursor.lastrowid or 0)

    def load_adaptive_rules(self) -> List[Dict]:
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT id, created_at_utc, rule_type, affected_setup, check_for, check_direction, threshold,
                   description, example, active, sample_size, wins_blocked_est, losses_prevented_est,
                   times_triggered, trades_blocked, false_positives, last_triggered_utc, expires_at_utc, status
            FROM adaptive_rules
            ORDER BY id ASC
            """
        )
        rows = []
        for r in cursor.fetchall():
            rows.append(
                {
                    "id": int(r[0]),
                    "created_at_utc": self._parse_db_datetime(r[1]),
                    "rule_type": str(r[2] or ""),
                    "affected_setup": str(r[3] or ""),
                    "check_for": str(r[4] or ""),
                    "check_direction": str(r[5] or ""),
                    "threshold": float(r[6] or 0.0),
                    "description": str(r[7] or ""),
                    "example": str(r[8] or ""),
                    "active": bool(r[9]),
                    "sample_size": int(r[10] or 0),
                    "wins_blocked_est": float(r[11] or 0.0),
                    "losses_prevented_est": float(r[12] or 0.0),
                    "times_triggered": int(r[13] or 0),
                    "trades_blocked": int(r[14] or 0),
                    "false_positives": int(r[15] or 0),
                    "last_triggered_utc": self._parse_db_datetime(r[16]),
                    "expires_at_utc": self._parse_db_datetime(r[17]),
                    "status": str(r[18] or "CANDIDATE"),
                }
            )
        return rows

    def save_rule_event(self, event: Dict) -> int:
        cursor = self.conn.cursor()
        cursor.execute(
            """
            INSERT INTO rule_events (
                rule_id, event_time_utc, symbol, setup_id, direction, decision, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(event.get("rule_id") or 0),
                event.get("event_time_utc") or datetime.utcnow(),
                str(event.get("symbol") or ""),
                str(event.get("setup_id") or ""),
                str(event.get("direction") or ""),
                str(event.get("decision") or ""),
                str(event.get("notes") or ""),
            ),
        )
        self.conn.commit()
        return int(cursor.lastrowid or 0)

    def count_matching_lessons(self, affected_setup: str, check_for: str) -> int:
        cursor = self.conn.cursor()
        setup_like = f"%{str(affected_setup or '').upper()}%"
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM learned_lessons
            WHERE UPPER(strongest_opposing_setup) = ?
              AND UPPER(entry_setups_json) LIKE ?
            """,
            (str(check_for or "").upper(), setup_like),
        )
        row = cursor.fetchone()
        return int(row[0] or 0) if row else 0

    def get_rule_events_count(self, rule_id: int) -> int:
        cursor = self.conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM rule_events WHERE rule_id = ?", (int(rule_id),))
        row = cursor.fetchone()
        return int(row[0] or 0) if row else 0

    def get_adaptive_learning_stats(self) -> Dict:
        cursor = self.conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM learned_lessons")
        lessons = int(cursor.fetchone()[0] or 0)
        cursor.execute("SELECT COUNT(*) FROM adaptive_rules")
        rules_total = int(cursor.fetchone()[0] or 0)
        cursor.execute("SELECT COUNT(*) FROM adaptive_rules WHERE status='ACTIVE' AND active=1")
        rules_active = int(cursor.fetchone()[0] or 0)
        cursor.execute("SELECT COUNT(*) FROM adaptive_rules WHERE status='CANDIDATE'")
        rules_candidate = int(cursor.fetchone()[0] or 0)
        cursor.execute("SELECT COUNT(*) FROM adaptive_rules WHERE status='DISABLED' OR active=0")
        rules_disabled = int(cursor.fetchone()[0] or 0)
        cursor.execute("SELECT COUNT(*) FROM rule_events WHERE decision='BLOCKED'")
        blocked = int(cursor.fetchone()[0] or 0)
        cursor.execute(
            """
            SELECT strongest_opposing_setup, COUNT(*) AS c
            FROM learned_lessons
            GROUP BY strongest_opposing_setup
            ORDER BY c DESC
            LIMIT 1
            """
        )
        row = cursor.fetchone()
        common_miss = str(row[0]) if row and row[0] else None
        return {
            "lessons_count": lessons,
            "rules_total": rules_total,
            "rules_active": rules_active,
            "rules_candidate": rules_candidate,
            "rules_disabled": rules_disabled,
            "rules_blocked_count": blocked,
            "most_common_miss": common_miss,
        }
    
    def close(self):
        """Close database connection"""
        if self.conn:
            self.conn.close()
