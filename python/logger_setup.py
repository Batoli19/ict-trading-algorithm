"""
Logger Setup — Rotating File + Colored Console Output
═══════════════════════════════════════════════════════
Sets up Python's logging system with:
    1. Rotating file handler  — writes to a log file with automatic rotation
       (when the file reaches 10 MB, it rotates and keeps 5 backups)
    2. Colored console handler — prints log messages to the terminal with
       color-coding by severity level for easy scanning

Color mapping:
    DEBUG    → white   (routine detail)
    INFO     → cyan    (normal operations)
    WARNING  → yellow  (something worth noting)
    ERROR    → red     (something broke)
    CRITICAL → magenta (system-level failure)
"""

import logging
import logging.handlers
from pathlib import Path


def setup_logger(name: str, log_file: Path, level=logging.INFO) -> logging.Logger:
    """
    Create and configure a logger with both file and console handlers.

    Args:
        name:     Logger name (appears in log messages as [NAME])
        log_file: Path to the log file (e.g. logs/bot.log)
        level:    Minimum log level to capture (default: INFO)

    Returns:
        Configured Logger instance.
    """
    logger = logging.getLogger(name)
    logger.setLevel(level)

    # Standard log format: timestamp + level + logger name + message
    fmt = logging.Formatter(
        fmt="%(asctime)s  %(levelname)-8s  [%(name)s]  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # ─── File handler (rotating) ──────────────────────────────────────
    # Writes all log messages to a file. When the file reaches 10 MB,
    # it's rotated (renamed to .1, .2, etc.) and a new file is started.
    # Keeps 5 backups, so max disk usage is ~50 MB for logs.
    fh = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=10_000_000, backupCount=5, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    # ─── Console handler (colored) ────────────────────────────────────
    # Writes colored output to the terminal for easy visual scanning.
    ch = logging.StreamHandler()
    ch.setFormatter(ColorFormatter())
    logger.addHandler(ch)

    # Also apply to the root logger so child loggers inherit these handlers
    logging.getLogger().setLevel(level)
    logging.getLogger().addHandler(fh)

    return logger


class ColorFormatter(logging.Formatter):
    """
    Custom log formatter that adds ANSI color codes to console output.

    Uses ANSI escape sequences to color log messages by severity:
        \\033[37m = white (DEBUG)
        \\033[36m = cyan  (INFO)
        \\033[33m = yellow (WARNING)
        \\033[31m = red   (ERROR)
        \\033[35m = magenta (CRITICAL)
        \\033[0m  = reset (back to normal)

    Note: These colors work in most modern terminals (PowerShell, CMD with
    ANSI support, bash, zsh). They won't render in basic cmd.exe on old Windows.
    """
    COLORS = {
        "DEBUG":    "\033[37m",   # white
        "INFO":     "\033[36m",   # cyan
        "WARNING":  "\033[33m",   # yellow
        "ERROR":    "\033[31m",   # red
        "CRITICAL": "\033[35m",   # magenta
    }
    RESET = "\033[0m"
    # Console format uses shorter time format (just HH:MM:SS)
    FMT   = "%(asctime)s  %(levelname)-8s  [%(name)-8s]  %(message)s"

    def format(self, record):
        """Apply color to the entire log line and format it."""
        color = self.COLORS.get(record.levelname, "")
        formatter = logging.Formatter(
            fmt     = color + self.FMT + self.RESET,
            datefmt = "%H:%M:%S"
        )
        return formatter.format(record)
