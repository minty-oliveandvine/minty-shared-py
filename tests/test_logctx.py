import io
import logging
import logging.config
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from minty_shared import logctx

LINE = re.compile(
    r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z (?P<level>\w+) "
    r"\[trace=(?P<trace>[^\]]+)\] \[user=(?P<user>[^|]+)\|entity=(?P<entity>[^|]+)\|step=(?P<step>[^\]]+)\] "
    r"(?P<message>.*)$",
    re.S,
)


@pytest.fixture(autouse=True)
def _clean_context():
    logctx.reset()
    yield
    logctx.reset()


def _record(msg="hello %s", args=("world",), exc_info=None, **extra):
    record = logging.LogRecord("svc", logging.INFO, __file__, 1, msg, args, exc_info)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_line_with_no_context_prints_dashes():
    line = logctx.MintyFormatter().format(_record())
    m = LINE.match(line)
    assert m, line
    assert m["level"] == "INFO"
    assert (m["trace"], m["user"], m["entity"], m["step"]) == ("-", "-", "-", "-")
    assert m["message"] == "hello world"


def test_line_with_full_context_and_utc_timestamp():
    user, entity = uuid.uuid4(), uuid.uuid4()
    logctx.start_trace("a1b2c3d4")
    logctx.bind(user=user, entity=entity)
    with logctx.log_step("payment.approve"):
        record = _record()
        record.created = 1792658535.123  # 2026-10-22T08:42:15.123Z
        record.msecs = 123.0
        line = logctx.MintyFormatter().format(record)
    assert line == (
        f"2026-10-22T08:42:15.123Z INFO [trace=a1b2c3d4] "
        f"[user={user}|entity={entity}|step=payment.approve] hello world"
    )


def test_record_values_win_over_context():
    logctx.start_trace("a1b2c3d4")
    line = logctx.MintyFormatter().format(_record(trace="ffffffff", step="other"))
    m = LINE.match(line)
    assert (m["trace"], m["step"]) == ("ffffffff", "other")


def test_exception_text_follows_the_line():
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        line = logctx.MintyFormatter().format(_record(exc_info=sys.exc_info()))
    first, rest = line.split("\n", 1)
    assert LINE.match(first)
    assert "Traceback" in rest and "ValueError: boom" in rest


@pytest.mark.parametrize("good", ["a1b2c3d4", "0123456789abcdef0123456789abcdef", "deadbeefcafe"])
def test_start_trace_accepts_well_formed_ids(good):
    assert logctx.start_trace(good) == good
    assert logctx.current()["trace"] == good


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "",
        "abc",
        "A1B2C3D4",
        "a1b2c3d4\nINFO forged",
        "a1b2c3d4] [user=x",
        "g" * 8,
        "a" * 33,
        " a1b2c3d4",
    ],
)
def test_start_trace_replaces_bad_ids(bad):
    trace = logctx.start_trace(bad)
    assert trace != bad
    assert re.fullmatch(r"[0-9a-f]{8}", trace)


def test_start_trace_clears_the_previous_request():
    logctx.start_trace("a1b2c3d4")
    logctx.bind(user="u", entity="e")
    logctx.set_default_step("route")
    logctx.start_trace("b1b2c3d4")
    assert logctx.current() == {"trace": "b1b2c3d4", "user": "-", "entity": "-", "step": "-"}


def test_bind_changes_only_what_is_given():
    logctx.bind(user="u1")
    logctx.bind(entity="e1")
    assert (logctx.current()["user"], logctx.current()["entity"]) == ("u1", "e1")


def test_log_step_nests_and_resets():
    logctx.set_default_step("bills.approve")
    with logctx.log_step("payment.approve"):
        with logctx.log_step("payment.charge"):
            assert logctx.current()["step"] == "payment.charge"
        assert logctx.current()["step"] == "payment.approve"
    assert logctx.current()["step"] == "bills.approve"


def test_default_step_never_overrides_a_named_one():
    with logctx.log_step("payment.approve"):
        logctx.set_default_step("route")
        assert logctx.current()["step"] == "payment.approve"


def test_outgoing_headers():
    assert logctx.outgoing_headers() == {}
    logctx.start_trace("a1b2c3d4")
    assert logctx.outgoing_headers() == {"X-Request-ID": "a1b2c3d4"}


def test_plain_thread_starts_empty_but_carry_context_keeps_the_trace():
    logctx.start_trace("a1b2c3d4")
    logctx.bind(user="u1")
    seen = {}

    def work(key):
        seen[key] = logctx.current()

    plain = threading.Thread(target=work, args=("plain",))
    carried = threading.Thread(target=logctx.carry_context(work), args=("carried",))
    plain.start(), carried.start()
    plain.join(), carried.join()
    assert seen["plain"]["trace"] == "-"
    assert seen["carried"]["trace"] == "a1b2c3d4" and seen["carried"]["user"] == "u1"


def test_concurrent_contexts_do_not_mix():
    def request(i):
        trace = logctx.start_trace(f"{i:08x}")
        logctx.bind(user=f"user-{i}")
        for _ in range(200):
            assert logctx.current()["trace"] == trace
            assert logctx.current()["user"] == f"user-{i}"
        return True

    with ThreadPoolExecutor(8) as pool:
        assert all(pool.map(request, range(64)))


def test_logging_config_writes_every_line_in_one_format(tmp_path):
    config = logctx.logging_config(
        {"svc": "core.log", "svc.http": "api.log"},
        log_dir=tmp_path,
        no_propagate=["svc.http"],
        level="DEBUG",
    )
    logging.config.dictConfig(config)
    stream = io.StringIO()
    logging.getLogger("svc").handlers[0].setStream(stream)
    try:
        logctx.start_trace("a1b2c3d4")
        logctx.bind(user="u1", entity="e1")
        logging.getLogger("svc").info("core %s", 1)
        logging.getLogger("svc.child").warning("child line")
        logging.getLogger("svc.http").info("GET /x 200 3ms")
        assert logging.getLogger("svc.http").propagate is False
        for handler in logging.getLogger("svc").handlers + logging.getLogger("svc.http").handlers:
            handler.flush()
        core = (tmp_path / "core.log").read_text(encoding="utf-8").splitlines()
        api = (tmp_path / "api.log").read_text(encoding="utf-8").splitlines()
        assert [LINE.match(x)["message"] for x in core] == ["core 1", "child line"]
        assert [LINE.match(x)["message"] for x in api] == ["GET /x 200 3ms"]
        assert all(LINE.match(x)["trace"] == "a1b2c3d4" for x in core + api)
        assert "[user=u1|entity=e1|step=-] core 1" in stream.getvalue()
    finally:
        for name in ("svc", "svc.http"):
            for handler in logging.getLogger(name).handlers:
                handler.close()
            logging.getLogger(name).handlers.clear()


def test_logging_config_rotates_and_configures_root(tmp_path):
    config = logctx.logging_config(
        {"": "app.log"}, log_dir=tmp_path, max_bytes=1000, backup_count=3
    )
    assert config["root"]["handlers"] == ["console", "file_app.log"]
    handler = config["handlers"]["file_app.log"]
    assert handler["class"] == "logging.handlers.RotatingFileHandler"
    assert (handler["maxBytes"], handler["backupCount"]) == (1000, 3)


def test_logging_config_without_log_dir_is_console_only():
    config = logctx.logging_config({"svc": "core.log"})
    assert config["loggers"]["svc"]["handlers"] == ["console"]
