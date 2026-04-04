"""
Rotating file logger for Glint bot.

Call setup_file_logging() once at startup (main.py does this).
All modules use standard logging.getLogger() — this just adds the file handler.
"""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LOG_DIR = Path(__file__).resolve().parent / "logs"
_LOG_FILE = _LOG_DIR / "bot.log"
_initialized = False


def setup_file_logging(level: int = logging.INFO):
    global _initialized
    if _initialized:
        return
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        _LOG_FILE,
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5,
        encoding="utf-8",
    )
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(
        "[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logging.getLogger().addHandler(handler)
    _initialized = True
