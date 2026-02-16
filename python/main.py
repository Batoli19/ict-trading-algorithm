"""
╔══════════════════════════════════════════════════════════════════════╗
║           ICT TRADING BOT - Main Controller                         ║
║   Markets: Forex + Stocks | Platform: MetaTrader 5                  ║
║   Strategy: ICT (FVG, Turtle Soup, Stop Hunt, Kill Zones)           ║
╚══════════════════════════════════════════════════════════════════════╝
"""

import asyncio
import logging
import signal
import sys
from datetime import datetime
from pathlib import Path

from bot_engine import TradingEngine
from config_loader import load_config
from logger_setup import setup_logger
from news_filter import NewsFilter
from dashboard import Dashboard

# ─── Bootstrap ────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent.parent
LOG_DIR  = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

logger = setup_logger("MAIN", LOG_DIR / "bot.log")

# ─── Graceful Shutdown ─────────────────────────────────────────────────────────
shutdown_event = asyncio.Event()

def handle_signal(sig, frame):
    logger.warning(f"⚠️  Signal {sig} received — initiating graceful shutdown...")
    shutdown_event.set()

signal.signal(signal.SIGINT,  handle_signal)
signal.signal(signal.SIGTERM, handle_signal)

# ─── Main ──────────────────────────────────────────────────────────────────────
async def main():
    logger.info("=" * 60)
    logger.info("🤖  ICT Trading Bot Starting...")
    logger.info(f"🕐  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)

    # Load config
    config = load_config(BASE_DIR / "config" / "settings.json")
    logger.info(f"✅  Config loaded | Pairs: {config['pairs']}")

    # Init components
    news_filter = NewsFilter(config["news"])
    engine      = TradingEngine(config, news_filter, shutdown_event)
    dashboard   = Dashboard(engine)

    # Start
    tasks = [
        asyncio.create_task(engine.run(),          name="engine"),
        asyncio.create_task(dashboard.run(),        name="dashboard"),
        asyncio.create_task(shutdown_event.wait(),  name="shutdown_watcher"),
    ]

    logger.info("🚀  All systems running. Bot is live.\n")

    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

    # Cleanup
    logger.info("🛑  Shutting down...")
    for task in pending:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    await engine.shutdown()
    logger.info("✅  Bot shut down cleanly.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[Interrupted by user]")
        sys.exit(0)
