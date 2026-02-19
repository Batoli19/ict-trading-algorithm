"""
Enhanced bot_engine.py snippet - add these methods to your TradingEngine class
"""

# Add this method to TradingEngine class:

def get_status(self) -> dict:
    """Extended status with pair biases and trade log for Command Center"""
    account   = self.mt5.get_account_info()
    positions = self.mt5.get_open_positions()
    stats     = self.risk.get_stats()
    upcoming  = self.news.get_upcoming(2)
    
    # Get H4 bias for all pairs
    pair_biases = {}
    for symbol in self.pairs:
        candles_h4 = self.mt5.get_candles(symbol, "H4", 50)
        if candles_h4 and len(candles_h4) >= 30:
            bias = self.strategy.get_htf_bias(candles_h4)
            pair_biases[symbol] = bias.value  # 'BULLISH', 'BEARISH', 'NEUTRAL'
    
    # Build trade log from journal
    trade_log = []
    for record in self.risk.journal[-20:]:  # Last 20 trades
        trade_log.append({
            'time': record.open_time.strftime('%H:%M:%S'),
            'symbol': record.symbol,
            'type': record.direction,
            'setup': record.setup_type,
            'pnl': record.pnl,
            'lot': record.volume,
        })
    
    return {
        "timestamp": datetime.utcnow().isoformat(),
        "account":   account,
        "positions": positions,
        "stats":     stats,
        "connected": self.mt5.connected,
        "pair_biases": pair_biases,
        "trade_log": trade_log,
        "upcoming_news": [
            {
                "time":     e.time.strftime("%H:%M"),
                "currency": e.currency,
                "impact":   e.impact,
                "title":    e.title,
            }
            for e in upcoming
        ],
    }
