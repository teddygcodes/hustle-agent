"""Verify the autouse logger isolation fixture from conftest.py.

Test runs should NOT write to bot/logs/bot.log. The autouse fixture
swaps glint.* loggers' handlers for a NullHandler at session start
and restores on teardown. Tests that want to assert on log output
use pytest's caplog.
"""
import logging
from pathlib import Path


def test_glint_main_logger_does_not_write_to_bot_log():
    """Logging from glint.main during a test should not append to bot/logs/bot.log."""
    bot_log = Path(__file__).resolve().parents[1] / "bot" / "logs" / "bot.log"
    size_before = bot_log.stat().st_size if bot_log.exists() else 0
    logger = logging.getLogger("glint.main")
    logger.error("SENTINEL_TEST_LOG_ENTRY_DO_NOT_WRITE_TO_FILE")
    size_after = bot_log.stat().st_size if bot_log.exists() else 0
    assert size_after == size_before, (
        f"Logger wrote to bot.log during test (size {size_before} -> {size_after}). "
        "Autouse logger isolation fixture is not effective."
    )


def test_caplog_still_captures_when_explicitly_used(caplog):
    """The fixture should not break pytest's caplog mechanism."""
    with caplog.at_level(logging.WARNING, logger="glint.main"):
        logger = logging.getLogger("glint.main")
        logger.warning("captured message")
    assert "captured message" in caplog.text


def test_glint_logger_has_null_handler_installed():
    """Sanity: the glint.main logger should have at least one handler attached
    (NullHandler installed by the autouse fixture)."""
    handlers = (
        logging.getLogger("glint.main").handlers
        + logging.getLogger("glint").handlers
        + logging.getLogger().handlers
    )
    assert any(isinstance(h, logging.NullHandler) for h in handlers), (
        "Expected NullHandler attached by autouse fixture on glint or root logger"
    )
