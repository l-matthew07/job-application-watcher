"""Client for the X API v2 recent-search endpoint.

``GET /2/tweets/search/recent`` returns public posts from the last 7 days,
10-100 per page, paginated with an opaque ``next_token``. Auth is an app-only
bearer token. Rate limits surface both proactively, through the
``x-rate-limit-remaining`` / ``x-rate-limit-reset`` response headers, and
reactively as a 429.

Why the API and not HTML scraping: pulling x.com without authentication
violates X's terms of service, and mechanically it means guest-token
acquisition, aggressive IP blocking and markup that rotates without notice.
:class:`Transport` exists so a different backend can be dropped in without the
rest of the pipeline knowing — but the default is the documented API.

Reference: https://docs.x.com/x-api/posts/recent-search and
https://docs.x.com/x-api/fundamentals/rate-limits
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Protocol

SEARCH_URL = "https://api.x.com/2/tweets/search/recent"

# Everything the pipeline actually reads. Requesting less keeps responses small;
# requesting more costs nothing but bandwidth, so this list is deliberately tight.
TWEET_FIELDS = (
    "id,text,author_id,created_at,lang,public_metrics,"
    "conversation_id,referenced_tweets"
)
USER_FIELDS = "id,username,name,verified,created_at,public_metrics"
EXPANSIONS = "author_id"

DEFAULT_TIMEOUT = 20
DEFAULT_MAX_RETRIES = 4
DEFAULT_BACKOFF_BASE = 2.0
# Never sleep longer than this waiting out a rate-limit window. A reset clock
# that's wrong (or a 24h cap masquerading as a 15m window) shouldn't wedge a
# run for hours.
MAX_RATE_LIMIT_SLEEP = 15 * 60
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


class XApiError(RuntimeError):
    """Any non-recoverable failure talking to the API."""

    def __init__(self, message: str, status: int | None = None, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


class XAuthError(XApiError):
    """401/403 — bad token, or a token whose access tier lacks this endpoint."""


class XRateLimitError(XApiError):
    """Rate limited, and retries were exhausted."""


@dataclass(frozen=True)
class Response:
    """Transport-agnostic HTTP response."""

    status: int
    headers: dict = field(default_factory=dict)
    payload: dict = field(default_factory=dict)
    text: str = ""

    def header_int(self, name: str) -> int | None:
        raw = self.headers.get(name) or self.headers.get(name.title())
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None


class Transport(Protocol):
    """Minimal HTTP surface the client needs. Swap it out in tests."""

    def get(self, url: str, params: dict, headers: dict, timeout: float) -> Response:
        ...


class RequestsTransport:
    """Default transport — ``requests``, already a dependency of this repo."""

    def __init__(self, session=None):
        import requests

        self._session = session or requests.Session()

    def get(self, url: str, params: dict, headers: dict, timeout: float) -> Response:
        resp = self._session.get(url, params=params, headers=headers, timeout=timeout)
        try:
            payload = resp.json()
        except ValueError:
            payload = {}
        return Response(
            status=resp.status_code,
            headers={k.lower(): v for k, v in resp.headers.items()},
            payload=payload,
            text=resp.text,
        )


class XSearchClient:
    """Paginating, rate-limit-aware reader for recent search.

    ``sleep`` and ``monotonic``/``now`` are injectable so the retry and
    rate-limit paths are testable without real waiting.
    """

    def __init__(
        self,
        bearer_token: str,
        transport: Transport | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base: float = DEFAULT_BACKOFF_BASE,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.time,
    ) -> None:
        if not bearer_token:
            raise XAuthError("a bearer token is required")
        self._token = bearer_token
        self._transport = transport or RequestsTransport()
        self._timeout = timeout
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._sleep = sleep
        self._now = now
        self.requests_made = 0

    @property
    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._token}",
            "User-Agent": "job-application-watcher/twitter-sentiment (+https://github.com)",
        }

    def _rate_limit_pause(self, resp: Response) -> float:
        """Seconds to wait for the current window to reset, clamped."""
        reset = resp.header_int("x-rate-limit-reset")
        if reset is None:
            return 0.0
        return max(0.0, min(reset - self._now(), MAX_RATE_LIMIT_SLEEP))

    def _respect_remaining(self, resp: Response) -> None:
        """Pre-emptively wait out a window we just exhausted.

        Cheaper than spending the next request on a guaranteed 429 and then
        having to back off anyway.
        """
        if resp.header_int("x-rate-limit-remaining") == 0:
            pause = self._rate_limit_pause(resp)
            if pause > 0:
                self._sleep(pause)

    def _request(self, params: dict) -> Response:
        last: Response | None = None

        for attempt in range(self._max_retries + 1):
            resp = self._transport.get(
                SEARCH_URL, params, self._headers, self._timeout
            )
            self.requests_made += 1
            last = resp

            if resp.status == 200:
                return resp
            if resp.status in (401, 403):
                raise XAuthError(
                    f"authentication failed ({resp.status}) — check X_BEARER_TOKEN "
                    "and that your access tier includes recent search",
                    resp.status,
                    resp.payload or resp.text,
                )
            if resp.status not in RETRYABLE_STATUSES:
                raise XApiError(
                    f"search failed with HTTP {resp.status}",
                    resp.status,
                    resp.payload or resp.text,
                )
            if attempt == self._max_retries:
                break

            if resp.status == 429:
                # Honour the reset clock if present, otherwise fall back to
                # exponential backoff.
                delay = self._rate_limit_pause(resp) or self._backoff_base ** attempt
            else:
                delay = self._backoff_base ** attempt
            self._sleep(delay)

        assert last is not None  # loop runs at least once
        if last.status == 429:
            raise XRateLimitError(
                f"rate limited after {self._max_retries + 1} attempts",
                last.status,
                last.payload or last.text,
            )
        raise XApiError(
            f"search failed with HTTP {last.status} after "
            f"{self._max_retries + 1} attempts",
            last.status,
            last.payload or last.text,
        )

    def search_recent(
        self,
        query: str,
        max_results: int = 100,
        max_pages: int = 3,
        since_id: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> Iterator[dict]:
        """Yield raw response payloads, one per page, up to ``max_pages``.

        Yielding the page (rather than accumulating posts) keeps the caller in
        charge of when to stop — useful when a ``since_id`` run turns out to
        have nothing new.
        """
        if not query:
            raise XApiError("query is empty")

        params = {
            "query": query,
            "max_results": max_results,
            "tweet.fields": TWEET_FIELDS,
            "user.fields": USER_FIELDS,
            "expansions": EXPANSIONS,
        }
        if since_id:
            params["since_id"] = since_id
        if start_time:
            params["start_time"] = start_time
        if end_time:
            params["end_time"] = end_time

        for _ in range(max_pages):
            resp = self._request(params)
            payload = resp.payload or {}
            meta = payload.get("meta") or {}

            if payload.get("data"):
                yield payload
            elif not meta.get("next_token"):
                # Genuinely empty result — stop rather than burn a page budget.
                if meta.get("result_count", 0) == 0:
                    return

            next_token = meta.get("next_token")
            if not next_token:
                return

            self._respect_remaining(resp)
            params = {**params, "next_token": next_token}
