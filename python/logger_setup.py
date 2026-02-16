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
