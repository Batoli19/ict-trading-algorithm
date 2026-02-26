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
from typing import Dict, Set

import MetaTrader5 as mt5

logger = logging.getLogger("ANALYZER")


class TradeAnalyzer:
    def __init__(self, engine):
        self.engine = engine
        self.mt5 = engine.mt5
        self.memory = engine.memory
        self.brain = engine.brain
        self._analyzed_keys: Set[str] = set()
    
    async def run(self):
        """Background loop that analyzes closed trades"""
        logger.info("🔬  Trade Analyzer started")
        self.engine.analyzer_running = True
        
        while not self.engine.shutdown.is_set():
            try:
                self.engine.analyzer_last_tick = datetime.utcnow()
                logger.info(
                    "ANALYZER_TICK ts=%s running=%s",
                    self.engine.analyzer_last_tick.isoformat(),
                    self.engine.analyzer_running,
                )
                await self.analyze_recent_closes()
            except Exception as e:
                logger.error(f"Analyzer error: {e}", exc_info=True)
            
            # Check every 30 seconds
            await asyncio.sleep(30)

        self.engine.analyzer_running = False

    def _deal_key(self, mt5_trade: Dict) -> str:
        return (
            f"pos:{mt5_trade.get('position_id')}|"
            f"ord:{mt5_trade.get('order_ticket')}|"
            f"deal:{mt5_trade.get('deal_ticket')}"
        )

    def _is_exit_deal(self, mt5_trade: Dict) -> bool:
        deal_entry = mt5_trade.get("entry")
        if deal_entry is None:
            return True
        return deal_entry in (mt5.DEAL_ENTRY_OUT, mt5.DEAL_ENTRY_OUT_BY)
    
    async def analyze_recent_closes(self):
        """Find and analyze recently closed trades"""
        # Get all closed trades from MT5 today
        today_trades = self.mt5.get_today_trades()
        sample = [
            {
                "symbol": t.get("symbol"),
                "position_id": t.get("position_id"),
                "order_ticket": t.get("order_ticket"),
                "deal_ticket": t.get("deal_ticket"),
                "entry": t.get("entry"),
                "profit": t.get("profit"),
                "time": t.get("time"),
            }
            for t in today_trades[:3]
        ]
        logger.info("ANALYZER_DEALS count=%s sample=%s", len(today_trades), sample)
        
        for mt5_trade in today_trades:
            if not self._is_exit_deal(mt5_trade):
                continue
            deal_key = self._deal_key(mt5_trade)
            
            # Skip if already analyzed
            if deal_key in self._analyzed_keys:
                continue
            
            record = self.memory.find_open_trade_for_exit(
                position_id=mt5_trade.get("position_id"),
                order_ticket=mt5_trade.get("order_ticket"),
                ticket=mt5_trade.get("order_ticket"),
                deal_ticket=mt5_trade.get("deal_ticket"),
            )
            
            if not record:
                logger.warning(
                    "NO_DB_MATCH symbol=%s position_id=%s order_ticket=%s deal_ticket=%s entry=%s profit=%s time=%s",
                    mt5_trade.get("symbol"),
                    mt5_trade.get("position_id"),
                    mt5_trade.get("order_ticket"),
                    mt5_trade.get("deal_ticket"),
                    mt5_trade.get("entry"),
                    mt5_trade.get("profit"),
                    mt5_trade.get("time"),
                )
                continue
            
            try:
                await self._analyze_closed_trade(mt5_trade, record)
                self._analyzed_keys.add(deal_key)
            except Exception as e:
                logger.error(f"Analyze closed trade failed: {e}", exc_info=True)
    
    async def _analyze_closed_trade(self, mt5_trade: dict, db_record: Dict):
        """Deep analysis of a closed trade"""
        ticket = db_record["ticket"]
        symbol = db_record.get("symbol")
        setup_type = db_record.get("setup_type")
        if not symbol or not setup_type:
            logger.warning(f"Analyzer skipped incomplete DB record: {db_record}")
            return
        
        # Get M5 candles for analysis
        candles_m5 = self.mt5.get_candles(symbol, "M5", 50)
        
        # Build trade record dict for brain analysis
        trade_record = {
            'ticket': ticket,
            'symbol': symbol,
            'direction': db_record.get("direction"),
            'setup_type': setup_type,
            'entry_price': db_record.get("entry_price"),
            'sl_price': db_record.get("sl_price"),
            'tp_price': db_record.get("tp_price"),
            'exit_price': mt5_trade['price'],
            'outcome': None  # Will be determined
        }
        
        # Use AI brain to analyze the exit
        try:
            analysis = self.brain.analyze_exit(trade_record, candles_m5)
        except Exception as e:
            logger.error(f"Brain exit analysis failed: {e}", exc_info=True)
            analysis = {
                "stop_hit_reason": None,
                "tp_hit_reason": None,
                "lessons_learned": None,
            }
        
        # Record the exit in memory
        pnl = mt5_trade['profit']
        exit_time_raw = mt5_trade.get("time")
        exit_time = None
        if isinstance(exit_time_raw, str):
            try:
                exit_time = datetime.fromisoformat(exit_time_raw.replace("Z", "+00:00")).replace(tzinfo=None)
            except Exception:
                exit_time = None

        try:
            updated = self.memory.record_exit(
                position_id=mt5_trade.get("position_id"),
                order_ticket=mt5_trade.get("order_ticket"),
                deal_ticket=mt5_trade.get("deal_ticket"),
                ticket=ticket,
                exit_price=mt5_trade['price'],
                pnl=pnl,
                exit_time=exit_time,
                stop_hit_reason=analysis['stop_hit_reason'],
                tp_hit_reason=analysis['tp_hit_reason'],
                lessons=analysis['lessons_learned']
            )
        except Exception as e:
            logger.error(f"record_exit failed: {e}", exc_info=True)
            raise
        if not updated:
            return
        logger.info(
            "EXIT_RECORDED: symbol=%s position_id=%s order=%s deal=%s pnl=%+.2f outcome=%s",
            symbol,
            mt5_trade.get("position_id"),
            mt5_trade.get("order_ticket"),
            mt5_trade.get("deal_ticket"),
            pnl,
            "WIN" if pnl > 0 else "LOSS" if pnl < 0 else "BREAKEVEN",
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
