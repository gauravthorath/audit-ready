"""The trace context and the log lines it decorates (Step 37).

Offline, no model. What is pinned: fields bound with `bind` appear on every
line logged inside the block and vanish after it; the context crosses a
thread started with `copy_context().run`; a client-supplied id is accepted
only when it looks like an id; both formatters carry context and fields; and
`span` logs `elapsed_ms` on success and on failure.
"""

from __future__ import annotations

import contextvars
import json
import logging
import threading

import pytest

from core import tracing
from core.tracing import (
    JsonFormatter, TextFormatter, bind, clean_id, configure_logging, current, log_event,
    new_id, span,
)

log = logging.getLogger("auditready.test")


@pytest.fixture(autouse=True)
def _logging():
    """Configure logging for these tests and put the root logger back afterwards.

    `configure_logging` is a process-wide change: it replaces the root
    handlers and lowers the level to INFO. Left in place it makes every later
    test in the session log at INFO — which cost 36 seconds in the Streamlit
    mount test, whose libraries are extremely chatty below WARNING. A test
    that reconfigures a global restores it.
    """
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    configure_logging(fmt="json", level="INFO")
    try:
        yield
    finally:
        for handler in root.handlers[:]:
            root.removeHandler(handler)
        for handler in saved_handlers:
            root.addHandler(handler)
        root.setLevel(saved_level)


def _records(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "auditready.test"]


def test_bind_is_scoped_and_nests():
    assert current() == {}
    with bind(request_id="r1"):
        assert current() == {"request_id": "r1"}
        with bind(run_id="x", ignored=None):
            assert current() == {"request_id": "r1", "run_id": "x"}
        assert current() == {"request_id": "r1"}
    assert current() == {}


def test_a_line_logged_inside_bind_carries_the_fields(caplog):
    caplog.set_level(logging.INFO)
    with bind(request_id="abc"):
        log_event(log, "thing.happened", n=3)
    rec = _records(caplog)[-1]
    assert rec.trace == {"request_id": "abc"}
    line = json.loads(JsonFormatter().format(rec))
    assert line["event"] == "thing.happened"
    assert line["request_id"] == "abc" and line["n"] == 3
    assert {"ts", "level", "logger"} <= set(line)


def test_the_context_crosses_a_thread_started_with_copy_context(caplog):
    caplog.set_level(logging.INFO)
    seen: dict[str, str] = {}

    def worker():
        seen.update(current())
        log_event(log, "worker.ran")

    with bind(request_id="thr"):
        t = threading.Thread(target=contextvars.copy_context().run, args=(worker,))
        t.start()
        t.join()
    assert seen == {"request_id": "thr"}
    assert _records(caplog)[-1].trace == {"request_id": "thr"}


def test_a_plain_thread_does_not_inherit_the_context():
    seen: dict[str, str] = {}

    def worker():
        seen.update(current())

    with bind(request_id="plain"):
        t = threading.Thread(target=worker)
        t.start()
        t.join()
    assert seen == {}  # the reason batch.py and runs.py copy the context on purpose


def test_client_ids_are_kept_only_when_they_look_like_ids():
    assert clean_id("abc123-DEF_456") == "abc123-DEF_456"
    assert clean_id("  a1b2  ") == "a1b2"
    assert clean_id(None) is None and clean_id("") is None
    assert clean_id("trace-me-01") == "trace-me-01"  # a human's id is fine
    assert clean_id("has space") is None
    assert clean_id("new\nline") is None
    assert clean_id('quote"') is None
    assert clean_id("x" * 65) is None
    minted = new_id()
    assert len(minted) == tracing.ID_LENGTH and clean_id(minted) == minted


def test_text_formatter_quotes_values_with_spaces(caplog):
    caplog.set_level(logging.INFO)
    with bind(pair="TCK-1001xISO9001-8.6"):
        log_event(log, "batch.pair", label="two words", verdict="gap")
    text = TextFormatter().format(_records(caplog)[-1])
    assert "batch.pair" in text
    assert 'pair=TCK-1001xISO9001-8.6' in text
    assert 'label="two words"' in text and "verdict=gap" in text


def test_span_logs_elapsed_on_success_and_on_failure(caplog):
    caplog.set_level(logging.INFO)
    with span(log, "work.unit", kind="a") as extra:
        extra["result"] = 42
    ok = _records(caplog)[-1]
    assert ok.fields["outcome"] == "ok" and ok.fields["kind"] == "a"
    assert ok.fields["result"] == 42 and isinstance(ok.fields["elapsed_ms"], int)

    with pytest.raises(ValueError):
        with span(log, "work.unit"):
            raise ValueError("nope")
    bad = _records(caplog)[-1]
    assert bad.levelno == logging.WARNING
    assert bad.fields["outcome"] == "error" and "ValueError: nope" in bad.fields["error"]


def test_configure_logging_installs_exactly_one_handler():
    """It replaces handlers rather than stacking them — every call, one handler."""
    configure_logging(fmt="text")
    configure_logging(fmt="json")
    root = logging.getLogger()
    ours = [h for h in root.handlers if isinstance(h.formatter, (JsonFormatter, TextFormatter))]
    assert len(ours) == 1 and isinstance(ours[0].formatter, JsonFormatter)
    assert logging.getLogger("uvicorn.access").disabled is True
