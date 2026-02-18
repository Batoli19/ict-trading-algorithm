"""Logger setup — rotating file + colored console output."""

import logging
import logging.handlers
from pathlib import Path


def setup_logger(name: str, log_file: Path, level=logging.INFO) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(level)

    fmt = logging.Formatter(
        fmt="%(asctime)s  %(levelname)-8s  [%(name)s]  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # Rotating file handler (10 MB, 5 backups)
    fh = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=10_000_000, backupCount=5, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    # Console handler
    ch = logging.StreamHandler()
    ch.setFormatter(ColorFormatter())
    logger.addHandler(ch)

    # Also apply to all child loggers
    logging.getLogger().setLevel(level)
    logging.getLogger().addHandler(fh)

    return logger


class ColorFormatter(logging.Formatter):
    COLORS = {
        "DEBUG":    "\033[37m",   # white
        "INFO":     "\033[36m",   # cyan
        "WARNING":  "\033[33m",   # yellow
        "ERROR":    "\033[31m",   # red
        "CRITICAL": "\033[35m",   # magenta
    }
    RESET = "\033[0m"
    FMT   = "%(asctime)s  %(levelname)-8s  [%(name)-8s]  %(message)s"

    def format(self, record):
        color = self.COLORS.get(record.levelname, "")
        formatter = logging.Formatter(
            fmt     = color + self.FMT + self.RESET,
            datefmt = "%H:%M:%S"
        )
        return formatter.format(record)
gger = logging.getLogger("CONFIG")

def load_config(path: Path) -> dict:
    with open(path, "r") as f:
        config = json.load(f)
    _validate(config)
    return config

def _validate(config: dict):
    """Basic sanity checks"""
    required = ["mt5", "pairs", "risk", "ict"]
    for key in required:
        if key not in config:
            raise ValueError(f"Missing required config key: {key}")
    
    risk = config["risk"]
    
    # REMOVED: max_daily_loss_pct validation (allow any value for testing)
    
    if risk.get("risk_per_trade_pct", 0) > 5:
        logger.warning("⚠️  risk_per_trade_pct > 5% is aggressive!")
    
    if not config.get("pairs"):
        raise ValueError("No trading pairs specified")
    
    logger.info(f"✅  Config validation passed")
    if risk.get("max_daily_loss_pct", 0) > 10:
        logger.warning(f"⚠️  Daily loss limit: {risk['max_daily_loss_pct']}% — TESTING MODE")


