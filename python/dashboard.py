"""
Dashboard
──────────
Live terminal dashboard showing:
  • Account equity / balance / P&L
  • Open positions
  • Today's trade stats
  • Upcoming news events
  • Bot status
"""

import asyncio
import os
import logging
from datetime import datetime

logger = logging.getLogger("DASH")


def clear():
    os.system("cls" if os.name == "nt" else "clear")


class Dashboard:
    def __init__(self, engine):
        self.engine  = engine
        self.refresh = 5  # seconds

    async def run(self):
        while True:
            try:
                self._render()
            except Exception as e:
                logger.debug(f"Dashboard render error: {e}")
            await asyncio.sleep(self.refresh)

    def _render(self):
        status = self.engine.get_status()
        clear()

        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        connected = "🟢 LIVE" if status["connected"] else "🔴 DISCONNECTED"
        acct  = status["account"]
        stats = status["stats"]
        positions = status["positions"]
        news = status["upcoming_news"]

        print("╔" + "═"*62 + "╗")
        print("║  🤖  ICT TRADING BOT                          " + " "*14 + "║")
        print("║  " + now + "  " + connected + " " * (62 - len(now) - len(connected) - 2) + "  ║")
        print("╠" + "═"*62 + "╣")

        # Account
        bal  = acct.get("balance",  0)
        eq   = acct.get("equity",   0)
        pnl  = acct.get("profit",   0)
        cur  = acct.get("currency", "USD")
        pnl_sign = "+" if pnl >= 0 else ""
        pnl_color = "\033[92m" if pnl >= 0 else "\033[91m"
        reset = "\033[0m"

        print(f"║  💰 ACCOUNT  Balance: {bal:>10.2f} {cur}  "
              f"Equity: {eq:>10.2f}  "
              f"P&L: {pnl_color}{pnl_sign}{pnl:>8.2f}{reset}  ║")
        print("╠" + "═"*62 + "╣")

        # Today's stats
        dp = stats.get("daily_pnl", 0)
        dp_sign = "+" if dp >= 0 else ""
        dp_color = "\033[92m" if dp >= 0 else "\033[91m"

        print(f"║  📊 TODAY    Trades: {stats.get('daily_trades',0):>3}  "
              f"Win rate: {stats.get('winrate',0):>5.1f}%  "
              f"Daily P&L: {dp_color}{dp_sign}{dp:>8.2f}{reset}  ║")
        print(f"║             All-time Trades: {stats.get('trades',0):>4}  "
              f"Total P&L: {stats.get('total_pnl',0):>+10.2f}  "
              f"Exp: {stats.get('expectancy',0):>+6.2f}  ║")
        print("╠" + "═"*62 + "╣")

        # Open positions
        print(f"║  📋 OPEN POSITIONS ({len(positions)})                         " + " "*12 + "║")
        if positions:
            print("║  Ticket   Symbol      Type  Vol    Entry      P&L        ║")
            print("║  " + "─"*58 + "  ║")
            for p in positions[:5]:
                pnl_p  = p.get("profit", 0)
                pnl_s  = "+" if pnl_p >= 0 else ""
                pc = "\033[92m" if pnl_p >= 0 else "\033[91m"
                line = (f"  {p['ticket']:<7}  {p['symbol']:<10}  "
                        f"{p['type']:<4}  {p['volume']:<5}  "
                        f"{p['open_price']:<10.5f}  "
                        f"{pc}{pnl_s}{pnl_p:<8.2f}{reset}")
                print(f"║{line}  ║")
        else:
            print("║  No open positions                                       ║")
        print("╠" + "═"*62 + "╣")

        # Upcoming news
        print(f"║  📰 UPCOMING NEWS (next 2h)                               ║")
        if news:
            for n in news[:4]:
                icon  = "🔴" if n["impact"] == "HIGH" else "🟡"
                title = n["title"][:28]
                line  = f"  {icon} {n['time']}  [{n['currency']}]  {title:<28}"
                print(f"║{line}  ║")
        else:
            print("║  No high-impact events in next 2 hours                   ║")

        print("╚" + "═"*62 + "╝")
        print("  Press Ctrl+C to stop the bot gracefully")
