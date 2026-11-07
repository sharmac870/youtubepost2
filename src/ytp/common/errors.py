"""Error taxonomy (LLD Section 9.4) and the HTTP retry policy (Section 9.1)."""
from __future__ import annotations

import random
import time
from typing import Callable, TypeVar

import httpx

T = TypeVar("T")


class PipelineError(Exception):
    """Base class. `error_class` is recorded on publish attempts and in metrics."""
    error_class = "unknown"


class Transient(PipelineError):
    error_class = "transient"


class Defer(PipelineError):
    """Not an error: try again later (quota, publish limit, kill switch, open breaker, still processing)."""
    error_class = "deferred"

    def __init__(self, reason: str, delay_s: int = 300):
        super().__init__(reason)
        self.delay_s = delay_s


class AuthError(PipelineError):
    """401 after one refresh, invalid_grant, Meta code 190: stop the platform and alert (runbook R1)."""
    error_class = "auth"


class Permanent(PipelineError):
    """Validation, policy, media spec or a non-retryable API error: no retries."""
    error_class = "permanent"


class UnknownOutcome(PipelineError):
    """A side effect may or may not have happened: reconcile() before anything else (LLD 5.4)."""
    error_class = "unknown_outcome"


RETRY_STATUS = {429, 500, 502, 503, 504, 529}


def retry_after(response: httpx.Response | None) -> float | None:
    if response is None:
        return None
    value = response.headers.get("retry-after")
    try:
        return float(value) if value else None
    except ValueError:
        return None


def with_retries(call: Callable[[], T], *, attempts: int = 3, base: float = 2.0, cap: float = 60.0,
                 sleep: Callable[[float], None] = time.sleep) -> T:
    """Exponential backoff with full jitter; honours Retry-After; only Transient errors are retried."""
    for attempt in range(1, attempts + 1):
        try:
            return call()
        except Transient as exc:
            if attempt == attempts:
                raise
            wait = getattr(exc, "retry_after", None) or random.uniform(0, min(cap, base * 2 ** (attempt - 1)))
            sleep(min(wait, cap))
    raise AssertionError("unreachable")


def raise_for_http(response: httpx.Response, what: str) -> None:
    if response.status_code < 400:
        return
    detail = response.text[:300]
    if response.status_code in RETRY_STATUS:
        exc = Transient(f"{what}: HTTP {response.status_code} {detail}")
        exc.retry_after = retry_after(response)  # type: ignore[attr-defined]
        raise exc
    if response.status_code == 401:
        raise AuthError(f"{what}: HTTP 401 {detail}")
    raise Permanent(f"{what}: HTTP {response.status_code} {detail}")


def http_client(connect: float = 10, read: float = 60) -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(read, connect=connect), follow_redirects=False,
                        headers={"User-Agent": "youtubeposts2/1.0"})
