"""
Shared Learning Database — Cross-Account Adaptive Learning
═════════════════════════════════════════════════════
Stores lessons learned and adaptive rules in a GLOBAL database
(separate from the per-account TradingMemoryDB). This allows
knowledge to be shared across multiple trading accounts.

Architecture:
    Each account has its own TradingMemoryDB (per-account trades).
    All accounts share ONE SharedLearningDB (global knowledge).

    Account A (demo) --\                           
    Account B (prop) ---+--> SharedLearningDB --> Shared rules & lessons
    Account C (live) --/                           

Database tables:
    global_lessons:      Lessons extracted from losing trades
                         (what went wrong, opposing signals missed)
    global_rules:        Adaptive rules derived from lessons
                         (e.g., "block FVG when opposing STOP_HUNT detected")
    global_rule_events:  Audit log of when rules triggered/blocked trades

Rule lifecycle:
    1. CANDIDATE:  Created from loss patterns, needs validation
    2. ACTIVE:     Validated (precision > 65%, sample > 10), actively blocking
    3. DISABLED:   Too many false positives, turned off

The bootstrap_from_account_memory() method can migrate existing
per-account data into the shared database on first run.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger("SHARED_LEARNING")


class SharedLearningDB:
    """
    Global SQLite database for cross-account learning.

    Stores lessons (why trades failed) and adaptive rules (patterns to avoid)
    that are shared across all trading accounts. This means if Account A
    learns that FVG trades fail when a STOP_HUNT is present on the opposing
    side, Account B benefits from that knowledge automatically.

    Usage:
        db = SharedLearningDB(Path("memory/shared_learning.db"), account_login=12345)
        db.save_learned_lesson({...})     # Store a lesson from a loss
        db.save_adaptive_rule({...})      # Create/update an adaptive rule
        rules = db.load_adaptive_rules()  # Load all rules for entry blocking
    """

    def __init__(self, db_path: Path, account_login: int = 0):
        """
        Args:
            db_path:       Path to the SQLite database file
            account_login: MT5 account login ID (tags lessons with their source)
        """
        self.db_path = Path(db_path)
        self.account_login = int(account_login or 0)
        self.conn: Optional[sqlite3.Connection] = None
        self._init_database()

    def _init_database(self):
        """
        Initialize the SQLite database with WAL journal mode and create
        all required tables if they don't exist.

        WAL (Write-Ahead Logging) allows concurrent reads while writing,
        important because the bot reads rules while the analyzer writes lessons.
        """
        attempts = 5
        last_error = None
        for attempt in range(1, attempts + 1):
            try:
                self.conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30.0)
                cursor = self.conn.cursor()
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA busy_timeout=30000")
                cursor.execute("PRAGMA synchronous=NORMAL")
                break
            except sqlite3.OperationalError as e:
                last_error = e
                if self.conn is not None:
                    try:
                        self.conn.close()
                    except Exception:
                        pass
                    self.conn = None
                if "locked" not in str(e).lower() or attempt >= attempts:
                    raise
                logger.warning(
                    "SHARED_DB_LOCK_WAIT path=%s attempt=%s/%s retry_in=1s err=%s",
                    self.db_path,
                    attempt,
                    attempts,
                    e,
                )
                time.sleep(1.0)
        else:
            raise last_error or sqlite3.OperationalError("database is locked")


        # Table 1: global_lessons — stores individual lessons from losing trades
        # Each lesson records what the bot expected, what actually happened,
        # what opposing signals were missed, and a summary of the lesson.
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS global_lessons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_account_login INTEGER,        -- Which account generated this lesson
                ticket INTEGER,                      -- MT5 deal ticket number
                symbol TEXT,                         -- Trading instrument (e.g. EURUSD)
                expected_direction TEXT,              -- What we thought would happen (BUY/SELL)
                actual_direction TEXT,                -- What actually happened
                entry_reasons_json TEXT,              -- Why we entered (JSON array)
                entry_setups_json TEXT,               -- Which setups triggered (JSON array)
                entry_confidence REAL,               -- Signal confidence at entry
                missed_opposing_signals_json TEXT,    -- Opposing signals we should have seen
                strongest_opposing_setup TEXT,        -- The strongest signal we missed
                opposing_confluence_count INTEGER,    -- How many opposing signals existed
                lesson_summary TEXT,                 -- Human-readable lesson text
                created_at_utc TIMESTAMP,
                htf_bias TEXT,                       -- H4 bias at time of entry
                kill_zone TEXT,                      -- Which session (London, NY, etc.)
                spread_pips REAL                     -- Spread at time of entry
            )
            """)
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS global_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_account_login INTEGER,
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
            CREATE TABLE IF NOT EXISTS global_rule_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_id INTEGER,
                event_time_utc TIMESTAMP,
                account_login INTEGER,
                symbol TEXT,
                setup_id TEXT,
                direction TEXT,
                decision TEXT,
                notes TEXT
            )
            """
        )

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_global_lessons_symbol_time ON global_lessons(symbol, created_at_utc)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_global_rules_status_setup ON global_rules(status, affected_setup)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_global_rule_events_rule_time ON global_rule_events(rule_id, event_time_utc)")
        self.conn.commit()

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

    def _table_count(self, table_name: str) -> int:
        cursor = self.conn.cursor()
        cursor.execute(f"SELECT COUNT(*) FROM {table_name}")
        row = cursor.fetchone()
        return int(row[0] or 0) if row else 0

    def save_learned_lesson(self, lesson: Dict) -> int:
        cursor = self.conn.cursor()
        cursor.execute(
            """
            INSERT INTO global_lessons (
                source_account_login, ticket, symbol, expected_direction, actual_direction,
                entry_reasons_json, entry_setups_json, entry_confidence,
                missed_opposing_signals_json, strongest_opposing_setup,
                opposing_confluence_count, lesson_summary, created_at_utc, htf_bias, kill_zone, spread_pips
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(lesson.get("source_account_login") or self.account_login or 0),
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
                lesson.get("created_at_utc") or datetime.now(timezone.utc),
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
            int(rule.get("source_account_login") or self.account_login or 0),
            rule.get("created_at_utc") or datetime.now(timezone.utc),
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
                UPDATE global_rules
                SET source_account_login=?, created_at_utc=?, rule_type=?, affected_setup=?, check_for=?, check_direction=?,
                    threshold=?, description=?, example=?, active=?, sample_size=?, wins_blocked_est=?, losses_prevented_est=?,
                    times_triggered=?, trades_blocked=?, false_positives=?, last_triggered_utc=?, expires_at_utc=?, status=?
                WHERE id=?
                """,
                (*payload, rule_id),
            )
            if int(cursor.rowcount or 0) > 0:
                self.conn.commit()
                return rule_id
            cursor.execute(
                """
                INSERT INTO global_rules (
                    id, source_account_login, created_at_utc, rule_type, affected_setup, check_for, check_direction, threshold,
                    description, example, active, sample_size, wins_blocked_est, losses_prevented_est,
                    times_triggered, trades_blocked, false_positives, last_triggered_utc, expires_at_utc, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (rule_id, *payload),
            )
            self.conn.commit()
            return rule_id

        cursor.execute(
            """
            INSERT INTO global_rules (
                source_account_login, created_at_utc, rule_type, affected_setup, check_for, check_direction, threshold,
                description, example, active, sample_size, wins_blocked_est, losses_prevented_est,
                times_triggered, trades_blocked, false_positives, last_triggered_utc, expires_at_utc, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            FROM global_rules
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
            INSERT INTO global_rule_events (
                rule_id, event_time_utc, account_login, symbol, setup_id, direction, decision, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(event.get("rule_id") or 0),
                event.get("event_time_utc") or datetime.now(timezone.utc),
                int(event.get("account_login") or self.account_login or 0),
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
            FROM global_lessons
            WHERE UPPER(strongest_opposing_setup) = ?
              AND UPPER(entry_setups_json) LIKE ?
            """,
            (str(check_for or "").upper(), setup_like),
        )
        row = cursor.fetchone()
        return int(row[0] or 0) if row else 0

    def get_rule_events_count(self, rule_id: int) -> int:
        cursor = self.conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM global_rule_events WHERE rule_id = ?", (int(rule_id),))
        row = cursor.fetchone()
        return int(row[0] or 0) if row else 0

    def get_adaptive_learning_stats(self) -> Dict:
        cursor = self.conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM global_lessons")
        lessons = int(cursor.fetchone()[0] or 0)
        cursor.execute("SELECT COUNT(*) FROM global_rules")
        rules_total = int(cursor.fetchone()[0] or 0)
        cursor.execute("SELECT COUNT(*) FROM global_rules WHERE status='ACTIVE' AND active=1")
        rules_active = int(cursor.fetchone()[0] or 0)
        cursor.execute("SELECT COUNT(*) FROM global_rules WHERE status='CANDIDATE'")
        rules_candidate = int(cursor.fetchone()[0] or 0)
        cursor.execute("SELECT COUNT(*) FROM global_rules WHERE status='DISABLED' OR active=0")
        rules_disabled = int(cursor.fetchone()[0] or 0)
        cursor.execute("SELECT COUNT(*) FROM global_rule_events WHERE decision='BLOCKED'")
        blocked = int(cursor.fetchone()[0] or 0)
        cursor.execute(
            """
            SELECT strongest_opposing_setup, COUNT(*) AS c
            FROM global_lessons
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

    def bootstrap_from_account_memory(self, account_memory) -> Dict:
        """
        One-time migration: copy lessons, rules, and events from a
        per-account TradingMemoryDB into this shared database.

        Only runs if the shared DB is empty (first-time setup).
        This allows existing accounts to contribute their learnings
        to the shared knowledge base.

        Args:
            account_memory: TradingMemoryDB instance with existing data

        Returns:
            Dict with bootstrapped=True/False and counts of migrated items.
        """
        result = {"bootstrapped": False, "lessons": 0, "rules": 0, "rule_events": 0}
        src_conn = getattr(account_memory, "conn", None)
        if src_conn is None:
            return result

        if self._table_count("global_lessons") > 0 or self._table_count("global_rules") > 0:
            return result

        src = src_conn.cursor()
        dst = self.conn.cursor()

        try:
            src.execute(
                """
                SELECT ticket, symbol, expected_direction, actual_direction, entry_reasons_json,
                       entry_setups_json, entry_confidence, missed_opposing_signals_json, strongest_opposing_setup,
                       opposing_confluence_count, lesson_summary, created_at_utc, htf_bias, kill_zone, spread_pips
                FROM learned_lessons
                """
            )
            lesson_rows = src.fetchall()
            for row in lesson_rows:
                dst.execute(
                    """
                    INSERT INTO global_lessons (
                        source_account_login, ticket, symbol, expected_direction, actual_direction, entry_reasons_json,
                        entry_setups_json, entry_confidence, missed_opposing_signals_json, strongest_opposing_setup,
                        opposing_confluence_count, lesson_summary, created_at_utc, htf_bias, kill_zone, spread_pips
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (self.account_login, *row),
                )

            src.execute(
                """
                SELECT id, created_at_utc, rule_type, affected_setup, check_for, check_direction, threshold,
                       description, example, active, sample_size, wins_blocked_est, losses_prevented_est,
                       times_triggered, trades_blocked, false_positives, last_triggered_utc, expires_at_utc, status
                FROM adaptive_rules
                """
            )
            rule_rows = src.fetchall()
            for row in rule_rows:
                dst.execute(
                    """
                    INSERT INTO global_rules (
                        id, source_account_login, created_at_utc, rule_type, affected_setup, check_for, check_direction, threshold,
                        description, example, active, sample_size, wins_blocked_est, losses_prevented_est,
                        times_triggered, trades_blocked, false_positives, last_triggered_utc, expires_at_utc, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (int(row[0]), self.account_login, *row[1:]),
                )

            src.execute(
                """
                SELECT rule_id, event_time_utc, symbol, setup_id, direction, decision, notes
                FROM rule_events
                """
            )
            event_rows = src.fetchall()
            for row in event_rows:
                dst.execute(
                    """
                    INSERT INTO global_rule_events (
                        rule_id, event_time_utc, account_login, symbol, setup_id, direction, decision, notes
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (int(row[0] or 0), row[1], self.account_login, row[2], row[3], row[4], row[5], row[6]),
                )

            self.conn.commit()
            result.update(
                {
                    "bootstrapped": True,
                    "lessons": len(lesson_rows),
                    "rules": len(rule_rows),
                    "rule_events": len(event_rows),
                }
            )
            return result
        except Exception:
            self.conn.rollback()
            logger.exception("SHARED_LEARNING_BOOTSTRAP_FAILED")
            return result

    def close(self):
        if self.conn:
            self.conn.close()
