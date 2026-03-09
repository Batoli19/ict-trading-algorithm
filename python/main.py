"""
Main Entry Point — ICT Trading Bot
════════════════════════════════════
This is where the bot starts. It loads the configuration, initializes
all subsystems (MT5 connection, news filter, trading engine, Dashboard API),
and starts the main event loop.

How the startup sequence works:
    1. Load settings.json → validated config dict
    2. Create the NewsFilter (fetches economic calendar for trade blocking)
    3. Create the TradingEngine (the brain that scans, enters, and manages trades)
    4. Start the DashboardAPI (Flask web server for monitoring at http://127.0.0.1:5000)
    5. Start the engine's main loop (runs until shutdown or Ctrl+C)

The engine internally runs 4 concurrent async tasks:
    - Scan loop:    checks all pairs for new signals every 10 seconds
    - Manage loop:  manages open positions (trailing, partials) every 5 seconds
    - News loop:    refreshes economic calendar every hour
    - Analyzer:     detects closed trades and updates learning system
"""

import asyncio
import logging
from pathlib import Path

# Import the core modules that make up the bot
from config_loader import load_config     # Loads and validates settings.json
from news_filter import NewsFilter         # Blocks trading around high-impact news
from bot_engine import TradingEngine       # The main trading logic orchestrator
from api_server import DashboardAPI        # Flask web dashboard for monitoring

logger = logging.getLogger("MAIN")


def setup_basic_logging():
    """
    Configure Python's logging to print timestamped messages to the console.
    This is the initial logging setup — the bot may later add file-based
    logging via logger_setup.py.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  [%(name)s]  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


async def main():
    """
    Async main function — initializes all components and starts the bot.

    This is async because the TradingEngine uses asyncio for its concurrent
    scan/manage/news loops. Using asyncio (instead of threads) ensures that
    all trading logic runs on a single thread, avoiding race conditions
    when accessing shared state like position data.
    """
    # ─── Load configuration ───────────────────────────────────────────
    # The config file is expected at ../config/settings.json relative to
    # this file's location (i.e., the python/ folder).
    cfg_path = Path(__file__).resolve().parent.parent / "config" / "settings.json"
    cfg = load_config(cfg_path)

    # ─── Create shutdown event ────────────────────────────────────────
    # This event is shared across all async tasks. When set (e.g., by
    # the /api/shutdown endpoint or Ctrl+C), all loops will stop.
    shutdown = asyncio.Event()

    # ─── Initialize the news filter ──────────────────────────────────
    # The news filter fetches today's economic calendar and blocks
    # trading 30 minutes before and 15 minutes after high-impact events.
    news_cfg = cfg.get("news", {})
    news = NewsFilter(news_cfg)

    # ─── Initialize the trading engine ────────────────────────────────
    # The engine is the core of the bot — it connects to MT5, scans
    # pairs for ICT setups, executes trades, and manages open positions.
    engine = TradingEngine(cfg, news, shutdown)

    # ─── Start the Dashboard API ──────────────────────────────────────
    # The API runs on a background thread (Flask isn't async), providing
    # a web dashboard at http://127.0.0.1:5000 for monitoring bot status,
    # open positions, and trade history.
    api_cfg = cfg.get("api", {})
    api_host = api_cfg.get("host", "127.0.0.1")
    api_port = int(api_cfg.get("port", 5000))

    api = DashboardAPI(engine=engine, host=api_host, port=api_port)
    api.run_async()  # Starts Flask on a daemon thread
    logger.info(f"Dashboard API running at http://{api_host}:{api_port}")

    # ─── Start the engine ─────────────────────────────────────────────
    # This blocks until shutdown.set() is called (via API, Ctrl+C, or error).
    await engine.run()


if __name__ == "__main__":
    setup_basic_logging()
    logger.info("🚀 Starting ICT Trading Bot...")

    # asyncio.run() creates a new event loop, runs main() until it
    # completes, then closes the loop. This is the standard Python way
    # to run an async entry point.
    asyncio.run(main())
