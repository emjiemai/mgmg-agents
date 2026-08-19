"""Shared HTTP plumbing: retries, backoff, and client-side rate limiting.

Every external API call in this project goes through ``request_with_retry`` so
retry behaviour is identical across SAP, amoCRM, Graph, Verifix and Telegram.
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any, Awaitable, Callable

import httpx

from integrations.common.logging_setup import setup_logging

log = setup_logging("http")

DEFAULT_ATTEMPTS = 3
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class RateLimiter:
    """Token-less async rate limiter: spaces calls to at most ``rps`` per second.

    amoCRM enforces 7 requests/second and blocks the integration on breach, so
    we pace ourselves rather than relying on retries.
    """

    def __init__(self, rps: float) -> None:
        """Args:
        rps: Maximum sustained requests per second.
        """
        self._min_interval = 1.0 / rps if rps > 0 else 0.0
        self._last_call = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Block until the next call is allowed to go out."""
        if self._min_interval <= 0:
            return
        async with self._lock:
            wait = self._min_interval - (time.monotonic() - self._last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = time.monotonic()


async def request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    attempts: int = DEFAULT_ATTEMPTS,
    base_delay: float = 1.0,
    rate_limiter: RateLimiter | None = None,
    on_auth_failure: Callable[[], Awaitable[None]] | None = None,
    **kwargs: Any,
) -> httpx.Response:
    """Perform an HTTP request with exponential backoff and jitter.

    Retries on connection/timeout errors and on retryable status codes
    (408, 425, 429, 5xx). Honours a ``Retry-After`` header when present.
    A 401 triggers ``on_auth_failure`` once (re-login) and one extra attempt —
    SAP B1 Service Layer sessions expire mid-run and this is the normal path
    back in.

    Args:
        client: The httpx client to use.
        method: HTTP verb.
        url: Absolute or client-relative URL.
        attempts: Total attempts including the first (default 3).
        base_delay: Seconds for the first backoff; doubles each retry.
        rate_limiter: Optional limiter to pace calls before each attempt.
        on_auth_failure: Awaitable invoked on 401 to refresh credentials.
        **kwargs: Passed through to ``client.request`` (json, params, headers…).

    Returns:
        The successful ``httpx.Response`` (status < 400, or a non-retryable 4xx
        that the caller is expected to interpret).

    Raises:
        httpx.HTTPStatusError: if the last attempt returned a retryable error.
        httpx.RequestError: if the last attempt failed at the transport level.
    """
    last_error: Exception | None = None
    reauthenticated = False

    for attempt in range(1, attempts + 1):
        if rate_limiter is not None:
            await rate_limiter.acquire()

        try:
            response = await client.request(method, url, **kwargs)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_error = exc
            log.warning("{} {} — transport error on attempt {}/{}: {}", method, url, attempt, attempts, exc)
        else:
            if response.status_code == 401 and on_auth_failure is not None and not reauthenticated:
                log.info("{} {} — 401, re-authenticating and retrying once", method, url)
                reauthenticated = True
                await on_auth_failure()
                continue

            if response.status_code not in RETRYABLE_STATUS:
                return response

            last_error = httpx.HTTPStatusError(
                f"{response.status_code} from {url}", request=response.request, response=response
            )
            log.warning(
                "{} {} — HTTP {} on attempt {}/{}", method, url, response.status_code, attempt, attempts
            )
            retry_after = _retry_after_seconds(response)
            if retry_after is not None and attempt < attempts:
                await asyncio.sleep(retry_after)
                continue

        if attempt < attempts:
            delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 0.3)
            await asyncio.sleep(delay)

    assert last_error is not None  # unreachable unless attempts < 1
    raise last_error


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """Read a ``Retry-After`` header as seconds, if present and numeric."""
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return min(float(value), 60.0)
    except ValueError:
        return None
