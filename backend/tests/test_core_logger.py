"""
Tests for the centralised debug-logging utilities (app/core/logger.py).

Scope:
- is_debug() reflects the module-level _is_debug flag.
- get_debug_logger(): returns a DEBUG-emitting callable when the flag is on,
  a zero-overhead no-op when it is off.
- RequestTimer: context manager that logs START/END with elapsed ms when
  debug is on, stays silent when off, never swallows exceptions, and
  accepts a custom logger.
- The import-time DEBUG=true branch (logging.basicConfig force=True, noisy
  third-party loggers quietened) via importlib.reload in a controlled
  save/restore cycle.

Mocking: `app.core.logger._is_debug` is monkeypatched to flip debug mode
without re-importing; the reload test monkeypatches the DEBUG env var and
restores module + logger levels in a finally block so the rest of the suite
is unaffected. No network, no DB.
"""

import logging
import re
import time

import pytest

from app.core import logger as logger_module
from app.core.logger import RequestTimer, get_debug_logger, is_debug


# ── is_debug ───────────────────────────────────────────────────────────


def test_is_debug_false_by_default():
    """The test environment runs with DEBUG unset — module imported in off mode."""
    assert is_debug() is False


def test_is_debug_reflects_module_flag(monkeypatch):
    monkeypatch.setattr(logger_module, "_is_debug", True)
    assert is_debug() is True
    monkeypatch.setattr(logger_module, "_is_debug", False)
    assert is_debug() is False


# ── get_debug_logger ───────────────────────────────────────────────────


def test_get_debug_logger_returns_noop_when_debug_off():
    fn = get_debug_logger("app.test.noop")
    result = fn("ignored %s", "args")
    assert result is None  # no-op callable, returns nothing


def test_noop_logger_emits_nothing(caplog):
    fn = get_debug_logger("app.test.noop")
    with caplog.at_level(logging.DEBUG, logger="app.test.noop"):
        fn("should never be logged %s", "x")
    assert not [r for r in caplog.records if r.name == "app.test.noop"]


def test_get_debug_logger_still_sets_logger_level_to_debug():
    """Even in non-debug mode the underlying logger is configured at DEBUG
    so records are not filtered before reaching the (absent) handler."""
    get_debug_logger("app.test.levels")
    assert logging.getLogger("app.test.levels").level == logging.DEBUG


def test_get_debug_logger_emits_debug_records_when_flag_on(monkeypatch, caplog):
    monkeypatch.setattr(logger_module, "_is_debug", True)
    fn = get_debug_logger("app.test.debug")
    with caplog.at_level(logging.DEBUG, logger="app.test.debug"):
        fn("hello %s", "world")
        fn("plain message")

    messages = [r.getMessage() for r in caplog.records if r.name == "app.test.debug"]
    assert "hello world" in messages
    assert "plain message" in messages
    assert all(r.levelno == logging.DEBUG for r in caplog.records)


def test_get_debug_logger_noop_takes_any_args(monkeypatch):
    """The no-op must accept arbitrary positional args without raising
    (its signature is (msg, *args), mirroring Logger.debug)."""
    monkeypatch.setattr(logger_module, "_is_debug", False)
    fn = get_debug_logger("app.test.noop2")
    fn("msg")
    fn("msg", 1, 2)
    fn("msg", "a", "b", "c")


# ── RequestTimer ───────────────────────────────────────────────────────


def test_request_timer_silent_when_debug_off(caplog):
    with caplog.at_level(logging.DEBUG, logger="app.debug.timer"):
        timer = RequestTimer("silent-op")
        with timer as entered:
            assert entered is timer  # __enter__ returns self
        # __exit__ returns None (falsy) — nothing logged, no exception
    assert not [r for r in caplog.records if r.name == "app.debug.timer"]


def test_request_timer_start_attribute_set():
    timer = RequestTimer("op")
    assert timer.start == 0.0
    with timer:
        assert timer.start > 0.0


def test_request_timer_default_logger_name():
    timer = RequestTimer("op")
    assert timer.logger is logging.getLogger("app.debug.timer")
    assert timer.label == "op"


def test_request_timer_accepts_custom_logger():
    custom = logging.getLogger("app.test.timer.custom")
    timer = RequestTimer("custom-op", logger=custom)
    assert timer.logger is custom


def test_request_timer_logs_start_and_end_when_debug_on(monkeypatch, caplog):
    monkeypatch.setattr(logger_module, "_is_debug", True)
    with caplog.at_level(logging.DEBUG, logger="app.debug.timer"):
        with RequestTimer("timed-op"):
            time.sleep(0.005)

    messages = [r.getMessage() for r in caplog.records if r.name == "app.debug.timer"]
    start_msgs = [m for m in messages if "START" in m]
    end_msgs = [m for m in messages if "END" in m]
    assert len(start_msgs) == 1
    assert len(end_msgs) == 1
    assert "timed-op" in start_msgs[0]
    assert "timed-op" in end_msgs[0]

    # Elapsed milliseconds is parsed from the END line and is positive
    match = re.search(r"\((\d+(?:\.\d+)?) ms\)", end_msgs[0])
    assert match is not None, f"no elapsed ms in {end_msgs[0]!r}"
    assert float(match.group(1)) > 0


def test_request_timer_logs_to_custom_logger_when_debug_on(monkeypatch, caplog):
    monkeypatch.setattr(logger_module, "_is_debug", True)
    custom = logging.getLogger("app.test.timer.custom")
    with caplog.at_level(logging.DEBUG, logger="app.test.timer.custom"):
        with RequestTimer("custom-timed", logger=custom):
            pass

    messages = [r.getMessage() for r in caplog.records if r.name == "app.test.timer.custom"]
    assert any("START" in m for m in messages)
    assert any("END" in m for m in messages)


def test_request_timer_does_not_swallow_exceptions(monkeypatch):
    """__exit__ must return None so exceptions propagate through the with-block."""
    monkeypatch.setattr(logger_module, "_is_debug", True)
    timer = RequestTimer("failing-op")
    with pytest.raises(ValueError, match="kaput"):
        with timer:
            raise ValueError("kaput")
    assert timer.__exit__(ValueError, ValueError("again"), None) is None


# ── Import-time DEBUG=true branch (lines 29-41) via reload ────────────


def test_import_time_debug_branch_via_reload(monkeypatch, capsys):
    """Reloading the module with DEBUG=true runs the debug basicConfig branch:
    root at DEBUG on stdout, app logger at DEBUG, noisy loggers at WARNING."""
    import importlib

    watched = ("app", "httpx", "httpcore", "urllib3")
    saved_levels = {name: logging.getLogger(name).level for name in watched}
    saved_root_level = logging.getLogger().level

    monkeypatch.setenv("DEBUG", "true")
    try:
        reloaded = importlib.reload(logger_module)
        assert reloaded.is_debug() is True

        # The active debug logger now emits through the forced stdout config
        log = reloaded.get_debug_logger("app.test.reload")
        log("reloaded %s", "works")
        out = capsys.readouterr().out
        assert "app.test.reload" in out
        assert "reloaded works" in out
        assert "DEBUG" in out

        # Noisy third-party loggers quietened to WARNING; app at DEBUG
        for noisy in ("httpx", "httpcore", "urllib3"):
            assert logging.getLogger(noisy).level == logging.WARNING
        assert logging.getLogger("app").level == logging.DEBUG
        assert logging.getLogger().level == logging.DEBUG
    finally:
        monkeypatch.undo()
        importlib.reload(logger_module)  # restore the module to DEBUG=false

    assert logger_module.is_debug() is False
    # The non-debug reload resets the root logger but not named loggers —
    # restore the exact pre-test levels for a pristine suite.
    logging.getLogger().setLevel(saved_root_level)
    for name, level in saved_levels.items():
        logging.getLogger(name).setLevel(level)
