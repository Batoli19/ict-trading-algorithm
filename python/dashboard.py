"""
Dashboard — Live Terminal Status Display
══════════════════════════════════════════
Renders a real-time terminal dashboard showing:
    • Account balance / equity / P&L
    • Open positions with live P&L
    • Today's trade statistics
    • Upcoming high-impact news events
    • Bot connection status

The dashboard refreshes every 5 seconds, clearing the terminal and
re-rendering the entire display. It uses Unicode box-drawing characters
(╔═╗║╚═╝) for a clean, professional look.

Note: This is the TERMINAL dashboard (printed to console).
      The WEB dashboard is served by api_server.py via Flask.

Color codes used:
    \\033[92m = bright green  (positive P&L)
    \\033[91m = bright red    (negative P&L)
    \\033[0m  = reset
"""

import asyncio
import os
import logging
from datetime import datetime

logger = logging.getLogger("DASH")


def clear():
    """
    Clear the terminal screen.
    Uses 'cls' on Windows, 'clear' on Linux/Mac.
    """
    os.system("cls" if os.name == "nt" else "clear")


class Dashboard:
    """
    Live terminal dashboard that updates every 5 seconds.

    Gets its data from engine.get_status(), which returns a dict with:
        - connected: bool
        - account: { balance, equity, profit, currency }
        - stats: { daily_trades, daily_pnl, trades, total_pnl, winrate, expectancy }
        - positions: [ { ticket, symbol, type, volume, open_price, profit } ]
        - upcoming_news: [ { time, currency, impact, title } ]

    Started as a background task in the engine's run() method.
    """

    def __init__(self, engine):
        """
        Args:
            engine: TradingEngine instance (used to get status data)
        """
        self.engine  = engine
        self.refresh = 5  # Refresh interval in seconds

    async def run(self):
        """
        Async loop that re-renders the dashboard every 5 seconds.
        Runs forever until the bot shuts down.
        """
        while True:
            try:
                self._render()
            except Exception as e:
                # Never crash the bot because of a display error
                logger.debug(f"Dashboard render error: {e}")
            await asyncio.sleep(self.refresh)

    def _render(self):
        """
        Clear the terminal and render the full dashboard display.

        Layout:
            ╔══════════════════════════════════════════════════════════════╗
            ║  🤖  ICT TRADING BOT                                       ║
            ║  2025-03-07 14:30:00 UTC  🟢 LIVE                         ║
            ╠══════════════════════════════════════════════════════════════╣
            ║  💰 ACCOUNT  Balance: $5000   Equity: $5050   P&L: +$50   ║
            ╠══════════════════════════════════════════════════════════════╣
            ║  📊 TODAY    Trades: 3   Win rate: 66.7%   Daily P&L: +$75║
            ╠══════════════════════════════════════════════════════════════╣
            ║  📋 OPEN POSITIONS (1)                                      ║
            ║  Ticket  Symbol    Type  Vol    Entry      P&L              ║
            ╠══════════════════════════════════════════════════════════════╣
            ║  📰 UPCOMING NEWS (next 2h)                                 ║
            ╚══════════════════════════════════════════════════════════════╝
        """
        # Get current status from the engine
        status = self.engine.get_status()
        clear()

        # ─── Header ──────────────────────────────────────────────────
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

        # ─── Account section ─────────────────────────────────────────
        bal  = acct.get("balance",  0)
        eq   = acct.get("equity",   0)
        pnl  = acct.get("profit",   0)
        cur  = acct.get("currency", "USD")
        pnl_sign = "+" if pnl >= 0 else ""
        pnl_color = "\033[92m" if pnl >= 0 else "\033[91m"  # Green for profit, red for loss
        reset = "\033[0m"

        print(f"║  💰 ACCOUNT  Balance: {bal:>10.2f} {cur}  "
              f"Equity: {eq:>10.2f}  "
              f"P&L: {pnl_color}{pnl_sign}{pnl:>8.2f}{reset}  ║")
        print("╠" + "═"*62 + "╣")

        # ─── Today's stats ───────────────────────────────────────────
        dp = stats.get("daily_pnl", 0)
        daily_wr = stats.get("daily_winrate", stats.get("winrate", 0))
        dp_sign = "+" if dp >= 0 else ""
        dp_color = "\033[92m" if dp >= 0 else "\033[91m"

        print(f"║  📊 TODAY    Trades: {stats.get('daily_trades',0):>3}  "
              f"Win rate: {daily_wr:>5.1f}%  "
              f"Daily P&L: {dp_color}{dp_sign}{dp:>8.2f}{reset}  ║")
        print(f"║             All-time Trades: {stats.get('trades',0):>4}  "
              f"Total P&L: {stats.get('total_pnl',0):>+10.2f}  "
              f"Exp: {stats.get('expectancy',0):>+6.2f}  ║")
        print("╠" + "═"*62 + "╣")

        # ─── Open positions ──────────────────────────────────────────
        print(f"║  📋 OPEN POSITIONS ({len(positions)})                         " + " "*12 + "║")
        if positions:
            print("║  Ticket   Symbol      Type  Vol    Entry      P&L        ║")
            print("║  " + "─"*58 + "  ║")
            # Show at most 5 positions to keep the display compact
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

        # ─── Upcoming news ───────────────────────────────────────────
        print(f"║  📰 UPCOMING NEWS (next 2h)                               ║")
        if news:
            for n in news[:4]:  # Show at most 4 events
                icon  = "🔴" if n["impact"] == "HIGH" else "🟡"
                title = n["title"][:28]  # Truncate long titles
                line  = f"  {icon} {n['time']}  [{n['currency']}]  {title:<28}"
                print(f"║{line}  ║")
        else:
            print("║  No high-impact events in next 2 hours                   ║")

        # ─── Footer ──────────────────────────────────────────────────
        print("╚" + "═"*62 + "╝")
        print("  Press Ctrl+C to stop the bot gracefully")
