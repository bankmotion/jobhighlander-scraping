"""Loguru logging setup (console + rotating files)."""
import sys
from pathlib import Path

from loguru import logger

from config import settings

_LOGS_DIR = Path(__file__).resolve().parent / "logs"


def setup_logger():
    # Windows consoles default to cp1252, which makes loguru raise
    # UnicodeEncodeError when a message (e.g. a job title) has characters outside
    # that codepage. Force UTF-8 with a safe fallback so no log line is ever lost.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    logger.remove()
    logger.add(
        sys.stdout,
        colorize=True,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>",
        level=settings.log_level,
    )
    _LOGS_DIR.mkdir(exist_ok=True)
    logger.add(
        _LOGS_DIR / "app.log",
        rotation="10 MB",
        retention="7 days",
        level=settings.log_level,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}",
    )
    logger.add(
        _LOGS_DIR / "errors.log",
        rotation="10 MB",
        retention="30 days",
        level="ERROR",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}",
    )
    return logger


log = setup_logger()
