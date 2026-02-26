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
from datetime import datetime, timedelta
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
        return deal_entry in (mt5.DEAL_ENTRY_OUT, getattr(mt5, "DEAL_ENTRY_OUT_BY", -999))
    
    async def analyze_recent_closes(self):
        """Find DB-recorded trades that are now closed and record exits."""
        open_db_trades = self.memory.get_open_trades()
        mt5_open_positions = self.mt5.get_open_positions()
        mt5_open_keys = set()
        for p in mt5_open_positions:
            tk = p.get("ticket")
            if tk is not None:
                try:
                    mt5_open_keys.add(int(tk))
                except Exception:
                    pass

        logger.info("ANALYZER_OPEN_DB_TRADES count=%s", len(open_db_trades))
        logger.info("ANALYZER_MT5_OPEN_POS count=%s", len(mt5_open_positions))

        now = datetime.utcnow()
        from_dt = now - timedelta(hours=48)
        deals = self.mt5.get_deals_between(from_dt, now)
        exit_deals = [d for d in deals if self._is_exit_deal(d)]

        for db_trade in open_db_trades:
            position_id = db_trade.get("position_id")
            ticket = db_trade.get("ticket")
            live_key = position_id if position_id is not None else ticket
            if live_key is None:
                continue
            try:
                live_key_int = int(live_key)
            except Exception:
                continue

            if live_key_int in mt5_open_keys:
                continue

            mt5_trade = self._find_exit_deal_for_db_trade(db_trade, exit_deals)
            if not mt5_trade:
                logger.warning(
                    "NO_DB_MATCH symbol=%s position_id=%s order_ticket=%s deal_ticket=%s entry=%s profit=%s time=%s",
                    db_trade.get("symbol"),
                    db_trade.get("position_id"),
                    db_trade.get("order_ticket"),
                    db_trade.get("deal_ticket"),
                    None,
                    None,
                    db_trade.get("entry_time"),
                )
                continue

            deal_key = self._deal_key(mt5_trade)
            if deal_key in self._analyzed_keys:
                continue

            try:
                await self._analyze_closed_trade(mt5_trade, db_trade)
                self._analyzed_keys.add(deal_key)
            except Exception as e:
                logger.error(f"Analyze closed trade failed: {e}", exc_info=True)

    def _find_exit_deal_for_db_trade(self, db_trade: Dict, deals: list[Dict]) -> Dict | None:
        position_id = db_trade.get("position_id")
        ticket = db_trade.get("ticket")
        order_ticket = db_trade.get("order_ticket")
        deal_ticket = db_trade.get("deal_ticket")
        symbol = db_trade.get("symbol")

        candidates = []
        for d in deals:
            if symbol and d.get("symbol") != symbol:
                continue
            d_pos = d.get("position_id")
            d_ord = d.get("order_ticket")
            d_deal = d.get("deal_ticket")
            if position_id is not None and d_pos == position_id:
                candidates.append((4, d))
                continue
            if position_id is not None and d_ord == position_id:
                candidates.append((3, d))
                continue
            if ticket is not None and d_pos == ticket:
                candidates.append((3, d))
                continue
            if order_ticket is not None and d_ord == order_ticket:
                candidates.append((2, d))
                continue
            if deal_ticket is not None and d_deal == deal_ticket:
                candidates.append((1, d))

        if not candidates:
            return None

        candidates.sort(key=lambda x: (x[0], x[1].get("time") or ""), reverse=True)
        return candidates[0][1]
    
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
        
        # Record exit first; analysis must never block persistence.
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
                stop_hit_reason=None,
                tp_hit_reason=None,
                lessons=None
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
        try:
            sym = symbol
            self.engine.cooldowns.on_exit(sym, float(pnl))
        except Exception as e:
            logger.error(f"Cooldown on_exit failed: {e}", exc_info=True)
        try:
            self.engine.hybrid_gate.on_trade_closed(
                symbol=symbol,
                pnl=float(pnl),
                direction=db_record.get("direction"),
                setup_type=db_record.get("setup_type"),
            )
        except Exception:
            pass
        try:
            self.engine.risk.on_trade_closed(
                symbol=symbol,
                outcome="WIN" if pnl > 0 else "LOSS" if pnl < 0 else "BREAKEVEN",
                pnl=float(pnl),
                exit_time=exit_time or datetime.utcnow(),
            )
        except Exception as e:
            logger.error(f"Risk on_trade_closed failed: {e}", exc_info=True)

        analysis = None
        try:
            analysis = self.brain.analyze_exit(trade_record, candles_m5)
        except Exception as e:
            logger.error(f"Brain exit analysis failed: {e}", exc_info=True)

        if analysis:
            try:
                self.memory.update_exit_analysis(
                    trade_id=db_record.get("id"),
                    ticket=ticket,
                    stop_hit_reason=analysis.get("stop_hit_reason"),
                    tp_hit_reason=analysis.get("tp_hit_reason"),
                    lessons=analysis.get("lessons_learned"),
                )
            except Exception as e:
                logger.error(f"update_exit_analysis failed: {e}", exc_info=True)
        
        # Log the learning
        outcome = "WIN" if pnl > 0 else "LOSS" if pnl < 0 else "BE"
        logger.info(f"🔬  Analyzed #{ticket} ({setup_type}): {outcome} | P&L: {pnl:+.2f}")
        
        if analysis and analysis.get('stop_hit_reason'):
            logger.info(f"   └─ Stop: {analysis['stop_hit_reason']}")
        if analysis and analysis.get('tp_hit_reason'):
            logger.info(f"   └─ TP: {analysis['tp_hit_reason']}")
        if analysis and analysis.get('lessons_learned'):
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
