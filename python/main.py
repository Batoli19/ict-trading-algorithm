import asyncio
import logging
from pathlib import Path

from config_loader import load_config
from news_filter import NewsFilter
from bot_engine import TradingEngine

logger = logging.getLogger("MAIN")


def setup_basic_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  [%(name)s]  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


async def main():
    # Adjust path depending on where you run from
    # If you run from /python folder, config is usually ../config/settings.json
    cfg_path = Path("../config/settings.json")

    cfg = load_config(cfg_path)

    shutdown = asyncio.Event()

    news_cfg = cfg.get("news", {})  # make sure your settings.json has "news": {...}
    news = NewsFilter(news_cfg)

    engine = TradingEngine(cfg, news, shutdown)
    await engine.run()


if __name__ == "__main__":
    setup_basic_logging()
    logger.info("🚀 Starting ICT Trading Bot...")
    asyncio.run(main())
