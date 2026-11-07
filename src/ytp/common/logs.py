"""JSON logs with a redaction filter (LLD Section 8.3 and 10.1).

Every record goes through `redact`, which removes bearer tokens, OAuth tokens, client secrets, API keys
and query-string tokens. tests/test_redaction.py proves a token never reaches the output.
Container Apps ships stdout to Log Analytics, so there is no separate logging agent to pay for.
"""
from __future__ import annotations

import json
import logging
import re
import sys
from contextvars import ContextVar
from datetime import datetime, timezone

context: ContextVar[dict] = ContextVar("log_context", default={})

PATTERNS = [
    (re.compile(r"(?i)(authorization\s*[:=]\s*)(bearer|oauth)?\s*[^\s,'\"}]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)\b(bearer|oauth)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 [REDACTED]"),
    (re.compile(r"(?i)((?:access_token|refresh_token|client_secret|api_key|apikey|key|appsecret_proof|token|password)"
                r"[\"']?\s*[:=]\s*[\"']?)[^\s&,'\"}]+"), r"\1[REDACTED]"),
    (re.compile(r"sk-ant-[A-Za-z0-9_-]{10,}"), "[REDACTED]"),
    (re.compile(r"\bya29\.[A-Za-z0-9._-]+"), "[REDACTED]"),
    (re.compile(r"\b1//[A-Za-z0-9._-]{20,}"), "[REDACTED]"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"), "[REDACTED]"),
    (re.compile(r"\b(?:IG|EAA)[A-Za-z0-9]{40,}"), "[REDACTED]"),
    (re.compile(r"(?i)(sig=)[^&\s]+"), r"\1[REDACTED]"),
]


def redact(text: str) -> str:
    for pattern, replacement in PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        if record.exc_info:
            message += "\n" + self.formatException(record.exc_info)
        data = {
            "at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact(message),
            **{k: redact(str(v)) for k, v in context.get().items()},
        }
        return json.dumps(data, ensure_ascii=False)


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = ()
        return True


def setup(level: str = "INFO", json_output: bool = True) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(RedactingFilter())
    handler.setFormatter(JsonFormatter() if json_output else logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for noisy in ("azure", "httpx", "httpcore", "urllib3", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def bind(**values) -> None:
    context.set({**context.get(), **values})


def clear() -> None:
    context.set({})
