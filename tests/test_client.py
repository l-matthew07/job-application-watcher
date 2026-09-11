import pytest

from twitter_sentiment.client import (
    MAX_RATE_LIMIT_SLEEP,
    SEARCH_URL,
    Response,
    XApiError,
    XAuthError,
    XRateLimitError,
    XSearchClient,
)


class FakeTransport:
    """Replays a scripted list of Responses and records every request."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params, headers, timeout):
        self.calls.append({
            "url": url, "params": dict(params),
            "headers": dict(headers), "timeout": timeout,
        })
        if not self.responses:
            raise AssertionError("transport called more times than scripted")
        return self.responses.pop(0)


def page(ids, next_token=None, users=None, **headers):
    meta = {"result_count": len(ids)}
    if next_token:
        meta["next_token"] = next_token
    payload = {
        "data": [{"id": str(i), "text": f"post {i}"} for i in ids],
        "meta": meta,
    }
    if users:
        payload["includes"] = {"users": users}
    return Response(status=200, headers=headers, payload=payload)


def build(responses, **kwargs):
    """Client wired to a fake transport and a sleep recorder."""
    slept = []
    transport = FakeTransport(responses)
    client = XSearchClient(
        "test-token", transport=transport,
        sleep=slept.append, now=lambda: 1_000.0,
        **kwargs,
    )
    return client, transport, slept


def test_search_sends_token_and_expected_fields():
    client, transport, _ = build([page([1])])

    list(client.search_recent("tesla lang:en"))
    call = transport.calls[0]

    assert call["url"] == SEARCH_URL
    assert call["headers"]["Authorization"] == "Bearer test-token"
    assert call["params"]["query"] == "tesla lang:en"
    assert call["params"]["expansions"] == "author_id"
    assert "public_metrics" in call["params"]["tweet.fields"]
    assert "created_at" in call["params"]["user.fields"]


def test_optional_params_are_only_sent_when_given():
    client, transport, _ = build([page([1])])

    list(client.search_recent("tesla", since_id="123", start_time="2026-09-01T00:00:00Z"))
    params = transport.calls[0]["params"]

    assert params["since_id"] == "123"
    assert params["start_time"] == "2026-09-01T00:00:00Z"
    assert "end_time" not in params


def test_pagination_follows_next_token():
    client, transport, _ = build([
        page([1, 2], next_token="tok1"),
        page([3, 4], next_token="tok2"),
        page([5]),
    ])

    pages = list(client.search_recent("tesla", max_pages=5))

    assert [p["meta"]["result_count"] for p in pages] == [2, 2, 1]
    assert "next_token" not in transport.calls[0]["params"]
    assert transport.calls[1]["params"]["next_token"] == "tok1"
    assert transport.calls[2]["params"]["next_token"] == "tok2"


def test_pagination_stops_at_max_pages():
    client, transport, _ = build([
        page([1], next_token="a"),
        page([2], next_token="b"),
    ])

    assert len(list(client.search_recent("tesla", max_pages=2))) == 2
    assert len(transport.calls) == 2


def test_empty_result_yields_nothing_and_stops():
    client, transport, _ = build([Response(200, payload={"meta": {"result_count": 0}})])

    assert list(client.search_recent("obscure", max_pages=3)) == []
    assert len(transport.calls) == 1


def test_exhausted_window_is_waited_out_before_the_next_page():
    client, _, slept = build([
        page([1], next_token="tok", **{
            "x-rate-limit-remaining": "0", "x-rate-limit-reset": "1060",
        }),
        page([2]),
    ])

    list(client.search_recent("tesla", max_pages=2))

    assert slept == [60.0]


def test_remaining_budget_does_not_trigger_a_wait():
    client, _, slept = build([
        page([1], next_token="tok", **{
            "x-rate-limit-remaining": "42", "x-rate-limit-reset": "1060",
        }),
        page([2]),
    ])

    list(client.search_recent("tesla", max_pages=2))

    assert slept == []


def test_rate_limit_sleep_is_clamped():
    client, _, slept = build([
        page([1], next_token="tok", **{
            "x-rate-limit-remaining": "0", "x-rate-limit-reset": "99999999",
        }),
        page([2]),
    ])

    list(client.search_recent("tesla", max_pages=2))

    assert slept == [MAX_RATE_LIMIT_SLEEP]


def test_stale_reset_clock_does_not_sleep_negative():
    client, _, slept = build([
        page([1], next_token="tok", **{
            "x-rate-limit-remaining": "0", "x-rate-limit-reset": "500",
        }),
        page([2]),
    ])

    list(client.search_recent("tesla", max_pages=2))

    assert slept == []


def test_429_is_retried_using_the_reset_clock():
    client, transport, slept = build([
        Response(429, headers={"x-rate-limit-reset": "1030"}, payload={"title": "Too Many"}),
        page([1]),
    ])

    assert len(list(client.search_recent("tesla"))) == 1
    assert slept == [30.0]
    assert len(transport.calls) == 2


def test_429_without_a_reset_header_falls_back_to_backoff():
    client, _, slept = build([Response(429), page([1])])

    list(client.search_recent("tesla"))

    assert slept == [1.0]  # backoff_base ** 0


def test_persistent_429_raises_rate_limit_error():
    client, transport, _ = build([Response(429)] * 3, max_retries=2)

    with pytest.raises(XRateLimitError, match="after 3 attempts"):
        list(client.search_recent("tesla"))
    assert len(transport.calls) == 3


def test_5xx_is_retried_with_exponential_backoff():
    client, _, slept = build([Response(503), Response(500), page([1])])

    list(client.search_recent("tesla"))

    assert slept == [1.0, 2.0]


def test_persistent_5xx_raises_after_retries():
    client, _, _ = build([Response(503)] * 3, max_retries=2)

    with pytest.raises(XApiError, match="HTTP 503 after 3 attempts"):
        list(client.search_recent("tesla"))


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failures_are_not_retried(status):
    client, transport, _ = build([Response(status, payload={"title": "Unauthorized"})])

    with pytest.raises(XAuthError, match="check X_BEARER_TOKEN"):
        list(client.search_recent("tesla"))
    assert len(transport.calls) == 1


def test_client_errors_are_not_retried():
    client, transport, _ = build([Response(400, payload={"title": "Invalid Request"})])

    with pytest.raises(XApiError, match="HTTP 400") as excinfo:
        list(client.search_recent("tesla"))

    assert excinfo.value.status == 400
    assert excinfo.value.body == {"title": "Invalid Request"}
    assert len(transport.calls) == 1


def test_request_counter_tracks_every_attempt():
    client, _, _ = build([Response(503), page([1], next_token="t"), page([2])])

    list(client.search_recent("tesla", max_pages=2))

    assert client.requests_made == 3


def test_empty_query_is_rejected_before_a_request():
    client, transport, _ = build([])

    with pytest.raises(XApiError, match="query is empty"):
        list(client.search_recent(""))
    assert transport.calls == []


def test_missing_token_is_rejected_at_construction():
    with pytest.raises(XAuthError, match="bearer token is required"):
        XSearchClient("")


def test_header_lookup_is_case_insensitive_and_safe():
    resp = Response(200, headers={"x-rate-limit-remaining": "not-a-number"})

    assert resp.header_int("x-rate-limit-remaining") is None
    assert resp.header_int("x-rate-limit-reset") is None
