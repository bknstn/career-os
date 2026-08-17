"""Structured, redacted process logging for source collection workflows."""

from __future__ import annotations

import json
import logging as stdlib_logging
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from io import TextIOBase
from math import isfinite
from urllib.parse import urlsplit

from career_os.domain.runs import RunCounts

_HANDLER_MARKER = "career_os_json_handler"
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)\b(password|passwd|pwd|token|api[_-]?key|secret|authorization|cookie|"
    r"access[_-]?key)\s*([:=])\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;&]+)"
)
_BEARER_TOKEN = re.compile(r"(?i)\b(bearer)\s+[^\s,;]+")
_URL = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s'\"]+", re.IGNORECASE)
_STRUCTURED_PAYLOAD = re.compile(r"(?s)(?:^|:\s+)[\[{].*[\]}]\s*$")
_FIELD_NAME_NORMALIZER = re.compile(r"[^a-z0-9]")
_SENSITIVE_FIELD_NAMES = frozenset(
    {
        "accesskey",
        "accesstoken",
        "apikey",
        "apitoken",
        "authorization",
        "cookie",
        "password",
        "passwd",
        "pwd",
        "secret",
        "token",
    }
)
_FORBIDDEN_FIELDS = frozenset(
    {
        "body",
        "description",
        "description_html",
        "description_text",
        "http_body",
        "payload",
        "raw_payload",
        "response_body",
        "traceback",
    }
)
_NORMALIZED_FORBIDDEN_FIELDS = frozenset(
    _FIELD_NAME_NORMALIZER.sub("", field_name) for field_name in _FORBIDDEN_FIELDS
)

type LogScalar = str | int | float | bool | None
type UtcClock = Callable[[], datetime]


def utcnow() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(UTC)


def _as_utc(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return _as_utc(value, field_name="timestamp").isoformat().replace("+00:00", "Z")


def _require_nonblank(value: str, *, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be blank")
    return normalized


def _is_sensitive_field_name(field_name: str) -> bool:
    """Recognize credential fields independent of separator or letter case."""
    normalized = _FIELD_NAME_NORMALIZER.sub("", field_name.lower())
    return (
        normalized in _SENSITIVE_FIELD_NAMES
        or normalized.endswith("token")
        or normalized.endswith("secret")
    )


def _is_forbidden_field_name(field_name: str) -> bool:
    """Reject normalized evidence-field variants and conservative suffixes."""
    normalized = _FIELD_NAME_NORMALIZER.sub("", field_name.lower())
    return normalized in _NORMALIZED_FORBIDDEN_FIELDS or normalized.endswith(
        ("body", "payload", "description", "traceback")
    )


def _redact_path_credentials(path: str) -> str:
    """Mask `/token/<value>`-style credentials while retaining ordinary paths."""
    segments = path.split("/")
    for index, segment in enumerate(segments[:-1]):
        if _is_sensitive_field_name(segment) and segments[index + 1]:
            segments[index + 1] = "[redacted]"
    return "/".join(segments)


def _redact_url(match: re.Match[str]) -> str:
    raw_url = match.group(0).rstrip(".,;:)")
    suffix = match.group(0)[len(raw_url) :]
    try:
        parsed = urlsplit(raw_url)
    except ValueError:
        return "[redacted-url]" + suffix
    if not parsed.hostname:
        return "[redacted-url]" + suffix

    host = parsed.hostname
    try:
        port = f":{parsed.port}" if parsed.port is not None else ""
    except ValueError:
        port = ""
    path = _redact_path_credentials(parsed.path or "")
    query = "?[redacted]" if parsed.query else ""
    return f"{parsed.scheme}://{host}{port}{path}{query}" + suffix


def sanitize_text(value: str) -> str:
    """Remove common credentials and response-body shapes from log text."""
    stripped = value.strip()
    lowered = stripped.lower()
    if _STRUCTURED_PAYLOAD.search(stripped):
        return "[redacted structured payload]"
    if "<html" in lowered or "<!doctype html" in lowered:
        return "[redacted html payload]"

    sanitized = _URL.sub(_redact_url, value)
    sanitized = _BEARER_TOKEN.sub(r"\1 [redacted]", sanitized)
    sanitized = _SENSITIVE_ASSIGNMENT.sub(r"\1\2[redacted]", sanitized)
    if len(sanitized) > 500:
        return sanitized[:500] + "…[truncated]"
    return sanitized


@dataclass(frozen=True, slots=True)
class SafeException:
    """An exception representation that is safe to include in JSON logs."""

    exception_type: str
    message: str


def safe_exception(error: BaseException) -> SafeException:
    """Return class and redacted message without traceback or exception repr."""
    return SafeException(
        exception_type=type(error).__name__,
        message=sanitize_text(str(error)) or "[empty exception message]",
    )


def _sanitize_scalar(value: object) -> LogScalar:
    if isinstance(value, str):
        return sanitize_text(value)
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise ValueError("log float values must be finite")
        return value
    raise TypeError("log fields must be scalar values")


def _sanitize_field(field_name: str, value: object) -> LogScalar:
    """Redact known credential fields before considering their value."""
    if _is_sensitive_field_name(field_name):
        return "[redacted]"
    return _sanitize_scalar(value)


class _JsonFormatter(stdlib_logging.Formatter):
    def __init__(self, clock: UtcClock) -> None:
        super().__init__()
        self._clock = clock

    def format(self, record: stdlib_logging.LogRecord) -> str:
        payload: dict[str, LogScalar] = {
            "timestamp": _timestamp(self._clock()),
            "level": record.levelname,
            "event": sanitize_text(str(getattr(record, "event", "log"))),
        }
        for field_name in ("run_id", "source_key", "source_kind"):
            value = getattr(record, field_name, None)
            if value is not None:
                payload[field_name] = sanitize_text(str(value))
        extra_fields = getattr(record, "structured_fields", {})
        if isinstance(extra_fields, dict):
            for field_name, value in extra_fields.items():
                if (
                    isinstance(field_name, str)
                    and field_name.isidentifier()
                    and not _is_forbidden_field_name(field_name)
                    and field_name not in payload
                ):
                    try:
                        payload[field_name] = _sanitize_field(field_name, value)
                    except (TypeError, ValueError):
                        continue
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def configure_logging(
    *,
    level: int | str = "INFO",
    stream: TextIOBase | None = None,
    logger_name: str = "career_os",
    clock: UtcClock = utcnow,
) -> stdlib_logging.Logger:
    """Configure one named JSON logger without changing the root logger.

    Reconfiguration updates the existing Career OS handler rather than adding
    another one.  ``stream`` and ``clock`` are injectable for deterministic
    tests; production callers normally use the defaults.
    """
    logger = stdlib_logging.getLogger(_require_nonblank(logger_name, field_name="logger_name"))
    logger.setLevel(level)
    logger.propagate = False

    handlers = [handler for handler in logger.handlers if getattr(handler, _HANDLER_MARKER, False)]
    if handlers:
        handler = handlers[0]
        for duplicate in handlers[1:]:
            logger.removeHandler(duplicate)
            duplicate.close()
        if not isinstance(handler, stdlib_logging.StreamHandler):
            raise RuntimeError("Career OS logging handler must be a stream handler")
        handler.setStream(stream if stream is not None else sys.stderr)
    else:
        handler = stdlib_logging.StreamHandler(stream if stream is not None else sys.stderr)
        setattr(handler, _HANDLER_MARKER, True)
        logger.addHandler(handler)

    handler.setLevel(level)
    handler.setFormatter(_JsonFormatter(clock))
    return logger


@dataclass(frozen=True, slots=True)
class SourceRunLogger:
    """Immutable logging context for exactly one configured source run."""

    logger: stdlib_logging.Logger
    run_id: str
    source_key: str
    source_kind: str
    started_at: datetime
    clock: UtcClock = utcnow

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _require_nonblank(self.run_id, field_name="run_id"))
        object.__setattr__(
            self,
            "source_key",
            _require_nonblank(self.source_key, field_name="source_key"),
        )
        object.__setattr__(
            self,
            "source_kind",
            _require_nonblank(self.source_kind, field_name="source_kind"),
        )
        object.__setattr__(
            self,
            "started_at",
            _as_utc(self.started_at, field_name="started_at"),
        )

    def started(self) -> None:
        """Emit the explicit start event for this source run."""
        self._emit("source_run_started", started_at=_timestamp(self.started_at))

    def event(
        self,
        event: str,
        *,
        level: int = stdlib_logging.INFO,
        **fields: LogScalar,
    ) -> None:
        """Emit a bounded scalar-only event with this run's fixed context."""
        self._emit(event, level=level, **fields)

    def finished(
        self,
        *,
        status: str,
        counts: RunCounts,
        finished_at: datetime | None = None,
        error: BaseException | None = None,
    ) -> None:
        """Emit the terminal event with duration, counters, and safe error data."""
        completed_at = _as_utc(finished_at or self.clock(), field_name="finished_at")
        duration_seconds = (completed_at - self.started_at).total_seconds()
        if duration_seconds < 0:
            raise ValueError("finished_at must not be earlier than started_at")
        if not isfinite(duration_seconds):
            raise ValueError("duration_seconds must be finite")

        fields: dict[str, LogScalar] = {
            "status": _require_nonblank(status, field_name="status"),
            "started_at": _timestamp(self.started_at),
            "finished_at": _timestamp(completed_at),
            "duration_seconds": duration_seconds,
            **counts.as_fields(),
        }
        level = stdlib_logging.INFO
        if error is not None:
            safe_error = safe_exception(error)
            fields["exception_type"] = safe_error.exception_type
            fields["error_message"] = safe_error.message
            level = stdlib_logging.ERROR
        self._emit("source_run_finished", level=level, **fields)

    def _emit(self, event: str, *, level: int = stdlib_logging.INFO, **fields: LogScalar) -> None:
        clean_event = sanitize_text(_require_nonblank(event, field_name="event"))
        reserved_fields = {"timestamp", "level", "event", "run_id", "source_key", "source_kind"}
        invalid_fields = sorted(reserved_fields.intersection(fields))
        if invalid_fields:
            raise ValueError(f"reserved log field(s): {', '.join(invalid_fields)}")

        clean_fields: dict[str, LogScalar] = {}
        for field_name, value in fields.items():
            if not field_name.isidentifier():
                raise ValueError(f"log field name must be an identifier: {field_name}")
            if _is_forbidden_field_name(field_name):
                raise ValueError(f"log field is not permitted: {field_name}")
            try:
                clean_fields[field_name] = _sanitize_field(field_name, value)
            except (TypeError, ValueError) as error:
                raise type(error)(f"log field {field_name}: {error}") from error

        self.logger.log(
            level,
            clean_event,
            extra={
                "event": clean_event,
                "run_id": self.run_id,
                "source_key": self.source_key,
                "source_kind": self.source_kind,
                "structured_fields": clean_fields,
            },
        )


def bind_source_run(
    logger: stdlib_logging.Logger,
    *,
    run_id: str,
    source_key: str,
    source_kind: str,
    started_at: datetime,
    clock: UtcClock = utcnow,
) -> SourceRunLogger:
    """Bind immutable run/source identifiers without storing global context."""
    return SourceRunLogger(
        logger=logger,
        run_id=run_id,
        source_key=source_key,
        source_kind=source_kind,
        started_at=started_at,
        clock=clock,
    )
