"""
Trade Analyzer — Background Learning Process
─────────────────────────────────────────────
Runs periodically to:
  • Detect closed trades
  • Analyze why they won/lost
  • Update AI memory with lessons
  • Adjust setup confidence scores
  
Add this to bot_engine as a background task.
"""

import asyncio
import logging
from datetime import datetime
from typing import Set

logger = logging.getLogger("ANALYZER")


class TradeAnalyzer:
    def __init__(self, engine):
        self.engine = engine
        self.mt5 = engine.mt5
        self.memory = engine.memory
        self.brain = engine.brain
        self._analyzed_tickets: Set[int] = set()
    
    async def run(self):
        """Background loop that analyzes closed trades"""
        logger.info("🔬  Trade Analyzer started")
        
        while not self.engine.shutdown.is_set():
            try:
                await self.analyze_recent_closes()
            except Exception as e:
                logger.error(f"Analyzer error: {e}", exc_info=True)
            
            # Check every 30 seconds
            await asyncio.sleep(30)
    
    async def analyze_recent_closes(self):
        """Find and analyze recently closed trades"""
        # Get all closed trades from MT5 today
        today_trades = self.mt5.get_today_trades()
        
        for mt5_trade in today_trades:
            ticket = mt5_trade["ticket"]
            
            # Skip if already analyzed
            if ticket in self._analyzed_tickets:
                continue
            
            # Get the trade record from memory database
            cursor = self.memory.conn.cursor()
            cursor.execute("""
            SELECT ticket, symbol, direction, setup_type, entry_price, sl_price, tp_price,
                   htf_bias, kill_zone, spread_pips, reason, conditions_met, expected_outcome,
                   outcome, exit_price
            FROM trades
            WHERE ticket = ?
            """, (ticket,))
            
            record = cursor.fetchone()
            
            if not record:
                # Trade not in our memory - might be manual or from before bot started
                continue
            
            # Check if already closed in memory
            if record[13]:  # outcome field
                self._analyzed_tickets.add(ticket)
                continue
            
            # This is a newly closed trade - analyze it!
            await self._analyze_closed_trade(ticket, mt5_trade, record)
            self._analyzed_tickets.add(ticket)
    
    async def _analyze_closed_trade(self, ticket: int, mt5_trade: dict, db_record: tuple):
        """Deep analysis of a closed trade"""
        symbol = db_record[1]
        setup_type = db_record[3]
        
        # Get M5 candles for analysis
        candles_m5 = self.mt5.get_candles(symbol, "M5", 50)
        
        # Build trade record dict for brain analysis
        trade_record = {
            'ticket': ticket,
            'symbol': symbol,
            'direction': db_record[2],
            'setup_type': setup_type,
            'entry_price': db_record[4],
            'sl_price': db_record[5],
            'tp_price': db_record[6],
            'exit_price': mt5_trade['price'],
            'outcome': None  # Will be determined
        }
        
        # Use AI brain to analyze the exit
        analysis = self.brain.analyze_exit(trade_record, candles_m5)
        
        # Record the exit in memory
        pnl = mt5_trade['profit']
        self.memory.record_exit(
            ticket=ticket,
            exit_price=mt5_trade['price'],
            pnl=pnl,
            stop_hit_reason=analysis['stop_hit_reason'],
            tp_hit_reason=analysis['tp_hit_reason'],
            lessons=analysis['lessons_learned']
        )
        
        # Log the learning
        outcome = "WIN" if pnl > 0 else "LOSS" if pnl < 0 else "BE"
        logger.info(f"🔬  Analyzed #{ticket} ({setup_type}): {outcome} | P&L: {pnl:+.2f}")
        
        if analysis['stop_hit_reason']:
            logger.info(f"   └─ Stop: {analysis['stop_hit_reason']}")
        if analysis['tp_hit_reason']:
            logger.info(f"   └─ TP: {analysis['tp_hit_reason']}")
        if analysis['lessons_learned']:
            logger.info(f"   💡 Lesson: {analysis['lessons_learned']}")
        
        # Get updated confidence for this setup
        new_confidence = self.brain.get_adaptive_confidence(setup_type)
        logger.info(f"   📊 {setup_type} confidence updated: {new_confidence:.1f}%")
        
        # Check if setup should be disabled
        if self.brain.should_disable_setup(setup_type):
            logger.warning(f"   ⚠️  {setup_type} will be DISABLED (< 60% confidence with 50+ trades)")


# Add this to bot_engine.py run() method:
"""
# In TradingEngine.run() method, add:

async def run(self):
    if not await self._startup():
        self.shutdown.set()
        return

    # Create analyzer
    from trade_analyzer import TradeAnalyzer
    analyzer = TradeAnalyzer(self)

    scan_task     = asyncio.create_task(self._scan_loop())
    manage_task   = asyncio.create_task(self._manage_loop())
    news_task     = asyncio.create_task(self._news_loop())
    analyzer_task = asyncio.create_task(analyzer.run())  # <-- Add this

    await self.shutdown.wait()

    scan_task.cancel()
    manage_task.cancel()
    news_task.cancel()
    analyzer_task.cancel()  # <-- Add this
"""
