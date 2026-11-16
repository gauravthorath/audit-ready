"""Correlation ids and structured logs - one id from the browser to the model call (NFR-5, NFR-6).

Until this module existed nothing in the engine logged. Uvicorn's access line
was the only trace of a request, and it carried no id, so a slow audit could
not be followed from the click that started it to the model call that took
eleven seconds. This module is the smallest thing that makes that walk
possible, and it is stdlib only so `core/` stays framework-free.

How it fits together
--------------------
- A **context bag**, held in a `contextvars.ContextVar`. `bind(request_id=…)`
  adds fields for the duration of a `with` block; `current()` reads them.
  Contextvars follow the code across `await`, and across threads when the
  thread is started with `contextvars.copy_context().run(...)` - which is what
  the API's audit thread and the batch runner's worker pool do, so a line
  logged deep inside `core.llm.chat()` still knows the request and the run
  that caused it.
- **Every log line carries the bag.** Two formatters: JSON (one object per
  line, what a container ships) and text (what a developer reads). Both append
  every bound field and every `extra` field, so `grep <request_id>` finds the
  http line, the run line and each `llm.call` line of one user action.
- `log_event(logger, "llm.call", model=…, elapsed_ms=…)` is how code logs: an
  event name plus fields, never a formatted sentence. Sentences cannot be
  aggregated; fields can.
- `span(logger, "batch.pair")` times a block and logs its `elapsed_ms` on the
  way out, success or failure.

Why not OpenTelemetry now. It is the right end state, and the bag here is the
same shape as OTel baggage, so moving to it later is additive: a tracer that
reads `current()` and emits spans. Today it would add a collector, an exporter
and a UI to a stack that has none. Why not structlog: one more dependency for
what forty lines of formatter do.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

# A tuple of pairs rather than a dict: the default must be immutable, or every
# context would share (and mutate) one object.
_bag: contextvars.ContextVar[tuple[tuple[str, Any], ...]] = contextvars.ContextVar(
    "auditready_trace", default=()
)

# Minted ids are hex, short enough to read aloud, long enough not to collide in
# a log. Accepted ids may also be UUIDs, ULIDs or a human's "trace-me-01":
# letters, digits, dot, dash, underscore - nothing that could break a log line.
ID_LENGTH = 16
_ID_ALPHABET = set("0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ._-")


def new_id() -> str:
    return uuid.uuid4().hex[:ID_LENGTH]


def clean_id(value: str | None, *, max_length: int = 64) -> str | None:
    """A client-supplied id is untrusted input: keep it only if it looks like an id.

    A header that carried a newline or a quote would land in every log line
    for the request, which is how log injection starts. Anything outside the
    id alphabet, or too long, is dropped and a fresh id minted instead.
    """
    if not value:
        return None
    value = value.strip()
    if not value or len(value) > max_length or any(c not in _ID_ALPHABET for c in value):
        return None
    return value


def current() -> dict[str, Any]:
    """The fields bound in this context, outermost first."""
    return dict(_bag.get())


@contextmanager
def bind(**fields: Any) -> Iterator[dict[str, Any]]:
    """Add fields to the context for the duration of the block. None values are skipped."""
    merged = dict(_bag.get())
    merged.update({k: v for k, v in fields.items() if v is not None})
    token = _bag.set(tuple(merged.items()))
    try:
        yield merged
    finally:
        _bag.reset(token)


def elapsed_ms(started: float) -> int:
    """Milliseconds since a `time.perf_counter()` reading, as an int for the wire."""
    return int(round((time.perf_counter() - started) * 1000))


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def log_event(logger: logging.Logger, event: str, level: int = logging.INFO, **fields: Any) -> None:
    """Log `event` with structured fields. The context bag is added by the formatter."""
    if logger.isEnabledFor(level):
        logger.log(level, event, extra={"fields": fields})


@contextmanager
def span(logger: logging.Logger, event: str, **fields: Any) -> Iterator[dict[str, Any]]:
    """Time a block and log it on exit with `elapsed_ms` and `outcome`.

    The yielded dict is for fields known only at the end (a verdict, a token
    count): add to it inside the block and they land on the line.
    """
    started = time.perf_counter()
    extra: dict[str, Any] = {}
    try:
        yield extra
    except BaseException as exc:
        log_event(logger, event, logging.WARNING, elapsed_ms=elapsed_ms(started),
                  outcome="error", error=f"{type(exc).__name__}: {exc}", **fields, **extra)
        raise
    else:
        log_event(logger, event, elapsed_ms=elapsed_ms(started), outcome="ok", **fields, **extra)


def _record_fields(record: logging.LogRecord) -> dict[str, Any]:
    fields = getattr(record, "fields", None)
    return dict(fields) if isinstance(fields, dict) else {}


def _record_trace(record: logging.LogRecord) -> dict[str, Any]:
    """The context as it was when the record was made, or now if the factory is not installed."""
    trace = getattr(record, "trace", None)
    return dict(trace) if isinstance(trace, dict) else current()


_factory_installed = False


def install_record_factory() -> None:
    """Make every LogRecord carry `record.trace = current()` from the moment it is created.

    Formatting can happen later and on another thread (pytest's caplog, a
    queue handler), by which time the context that produced the line is gone.
    Capturing at creation is what makes the bag reliable for any handler.
    """
    global _factory_installed
    if _factory_installed:
        return
    previous = logging.getLogRecordFactory()

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = previous(*args, **kwargs)
        record.trace = current()
        return record

    logging.setLogRecordFactory(factory)
    _factory_installed = True


class JsonFormatter(logging.Formatter):
    """One JSON object per line: timestamp, level, logger, event, context, fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        payload.update(_record_trace(record))
        payload.update(_record_fields(record))
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    """`HH:MM:SS.mmm LEVEL logger event k=v k=v` - the same fields, for a terminal."""

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created).strftime("%H:%M:%S.%f")[:-3]
        pairs = {**_record_trace(record), **_record_fields(record)}
        tail = " ".join(f"{k}={_text_value(v)}" for k, v in pairs.items())
        line = f"{ts} {record.levelname:<7} {record.name} {record.getMessage()}"
        if tail:
            line = f"{line} {tail}"
        if record.exc_info:
            line = f"{line}\n{self.formatException(record.exc_info)}"
        return line


def _text_value(value: Any) -> str:
    text = str(value)
    return json.dumps(text) if " " in text or not text else text


def configure_logging(fmt: str | None = None, level: str | None = None,
                      stream: Any = None) -> None:
    """Install one handler on the root logger. Idempotent; call it at process start.

    `AUDITREADY_LOG_FORMAT` is `text` (default, for a terminal) or `json` (for a
    container). `AUDITREADY_LOG_LEVEL` defaults to INFO. Uvicorn's access log is
    silenced because the API's own `http.request` line carries the request id
    and the access line does not; two lines per request, one of them useless,
    is how people stop reading logs.
    """
    install_record_factory()
    fmt = (fmt or os.getenv("AUDITREADY_LOG_FORMAT", "text")).lower()
    level_name = (level or os.getenv("AUDITREADY_LOG_LEVEL", "INFO")).upper()
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(getattr(logging, level_name, logging.INFO))
    logging.getLogger("uvicorn.access").disabled = True
    # Third-party chatter that would drown the trace at INFO.
    for noisy in ("httpx", "httpcore", "openai", "chromadb", "sentence_transformers", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # The hub client warns about anonymous requests on every weight load; the
    # weights are baked into the image and never downloaded at run time.
    for chatty in ("huggingface_hub", "transformers"):
        logging.getLogger(chatty).setLevel(logging.ERROR)
