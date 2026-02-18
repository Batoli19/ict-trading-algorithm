"""
Test Mode - Aggressive Trade Execution
───────────────────────────────────────
Forces a trade every 5 minutes with reasonable stops for efficiency testing.
Bypasses all filters: kill zones, news blocks, bias checks, etc.

⚠️ WARNING: Use on demo accounts only!
"""

import asyncio
import logging
import random
from datetime import datetime
from ict_strategy import Direction, SetupType, Signal

logger = logging.getLogger("TESTMODE")


class TestMode:
    def __init__(self, engine, interval_seconds=300):
        """
        Args:
            engine: TradingEngine instance
            interval_seconds: Time between forced trades (default 300 = 5 min)
        """
        self.engine = engine
        self.interval = interval_seconds
        self.enabled = False
        self._last_trade_time = None
        
    async def run(self):
        """Background loop that forces trades every N seconds"""
        logger.warning("⚠️  TEST MODE ACTIVATED — Will force trades every 5 min")
        logger.warning("⚠️  All filters BYPASSED (kill zones, news, bias, etc.)")
        
        while not self.engine.shutdown.is_set():
            try:
                await asyncio.sleep(self.interval)
                await self._force_trade()
            except Exception as e:
                logger.error(f"Test mode error: {e}", exc_info=True)
    
    async def _force_trade(self):
        """Generate and execute a random test trade"""
        # Pick a random pair
        symbol = random.choice(self.engine.pairs)
        
        # Get current price
        tick = self.engine.mt5.get_tick(symbol)
        if not tick:
            logger.warning(f"No tick data for {symbol}, skipping test trade")
            return
        
        current_price = tick['ask']
        
        # Random direction
        direction = random.choice([Direction.BULLISH, Direction.BEARISH])
        
        # Calculate reasonable stops based on pair
        pip_size = self._get_pip_size(symbol)
        
        # Tight stops for testing: 10-15 pips SL, 20-30 pips TP (1:2 RR)
        sl_pips = random.uniform(10, 15)
        tp_pips = sl_pips * random.uniform(2.0, 2.5)
        
        if direction == Direction.BULLISH:
            entry_price = current_price
            sl_price = entry_price - (sl_pips * pip_size)
            tp_price = entry_price + (tp_pips * pip_size)
        else:
            entry_price = current_price
            sl_price = entry_price + (sl_pips * pip_size)
            tp_price = entry_price - (tp_pips * pip_size)
        
        # Create signal
        signal = Signal(
            symbol=symbol,
            direction=direction,
            setup_type=SetupType.SCALP,  # Mark as scalp for testing
            entry=entry_price,
            sl=sl_price,
            tp=tp_price,
            confidence=0.50,  # Low confidence = test trade
            reason=f"TEST MODE: Random {direction.value} | SL:{sl_pips:.1f}p TP:{tp_pips:.1f}p"
        )
        
        logger.info(f"🎲 TEST TRADE: {symbol} {direction.value} @ {entry_price:.5f} | "
                    f"SL: {sl_price:.5f} ({sl_pips:.1f}p) | "
                    f"TP: {tp_price:.5f} ({tp_pips:.1f}p)")
        
        # Get account balance for position sizing
        account = self.engine.mt5.get_account_info()
        balance = account.get('balance', 5000)
        
        # Execute through engine
        await self.engine._execute_signal(signal, balance)
        self._last_trade_time = datetime.utcnow()
    
    def _get_pip_size(self, symbol: str) -> float:
        """Get pip size for a symbol"""
        if "JPY" in symbol:
            return 0.01
        if symbol in ("US30", "NAS100", "SPX500"):
            return 1.0
        if "XAU" in symbol or "GOLD" in symbol:
            return 0.1
        return 0.0001
