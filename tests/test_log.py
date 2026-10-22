import logging

import pytest

from minty_shared.log import BraceLogger, get_logger


@pytest.fixture
def capture(caplog):
    caplog.set_level(logging.DEBUG, logger="brace-test")
    return caplog


def _messages(caplog):
    return [r.getMessage() for r in caplog.records if r.name == "brace-test"]


def test_brace_placeholders(capture):
    get_logger("brace-test").info("renewals: {} issued for {}", 3, "u1")
    assert _messages(capture) == ["renewals: 3 issued for u1"]


def test_named_placeholders(capture):
    get_logger("brace-test").info("{n} issued", n=3)
    assert _messages(capture) == ["3 issued"]


def test_percent_placeholders(capture):
    get_logger("brace-test").warning("bill=%s failed", "b1")
    assert _messages(capture) == ["bill=b1 failed"]


def test_no_arguments_keeps_braces_as_written(capture):
    get_logger("brace-test").info("payload {'a': 1} kept")
    assert _messages(capture) == ["payload {'a': 1} kept"]


def test_bad_format_appends_the_arguments(capture):
    get_logger("brace-test").info("only {} here {}", 1)
    assert _messages(capture) == ["only {} here {} 1"]


def test_exception_carries_the_traceback(capture):
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        get_logger("brace-test").exception("failed {}", "x")
    (record,) = [r for r in capture.records if r.name == "brace-test"]
    assert record.levelno == logging.ERROR and record.exc_info


def test_levels_and_caller_location(capture):
    log = BraceLogger("brace-test")
    log.debug("d"), log.info("i"), log.warning("w"), log.error("e"), log.critical("c")
    levels = [r.levelname for r in capture.records if r.name == "brace-test"]
    assert levels == ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
    assert {r.filename for r in capture.records if r.name == "brace-test"} == {"test_log.py"}


def test_disabled_level_never_formats(capture):
    class Boom:
        def __format__(self, spec):
            raise AssertionError("formatted")

    logging.getLogger("brace-test").setLevel(logging.INFO)
    get_logger("brace-test").debug("{}", Boom())
    assert _messages(capture) == []
