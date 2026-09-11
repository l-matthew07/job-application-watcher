"""Shared fixtures: a scripted fake X API and a real scorer."""

import pytest

from twitter_sentiment.client import Response, XSearchClient
from twitter_sentiment.sentiment import SentimentScorer


class FakeXApi:
    """Serves scripted search pages and records the queries it was asked."""

    def __init__(self, pages=None):
        self.pages = list(pages or [])
        self.queries = []

    def get(self, url, params, headers, timeout):
        self.queries.append(dict(params))
        if not self.pages:
            return Response(200, payload={"meta": {"result_count": 0}})
        return Response(200, payload=self.pages.pop(0))


def make_post(post_id, text, author_id="100", lang="en", likes=0, created_at=None):
    return {
        "id": str(post_id),
        "text": text,
        "author_id": author_id,
        "lang": lang,
        "created_at": created_at or "2026-09-10T12:00:00.000Z",
        "public_metrics": {
            "like_count": likes, "retweet_count": 0,
            "reply_count": 0, "quote_count": 0, "impression_count": likes * 20,
        },
    }


def make_user(user_id="100", username="matthew", followers=5000,
              created_at="2018-01-01T00:00:00.000Z"):
    return {
        "id": str(user_id),
        "username": username,
        "name": username.title(),
        "created_at": created_at,
        "public_metrics": {
            "followers_count": followers, "following_count": 300, "tweet_count": 900,
        },
    }


def make_page(posts, users=None, next_token=None):
    meta = {"result_count": len(posts)}
    if posts:
        meta["newest_id"] = max((p["id"] for p in posts), key=int)
        meta["oldest_id"] = min((p["id"] for p in posts), key=int)
    if next_token:
        meta["next_token"] = next_token
    payload = {"data": posts, "meta": meta}
    if users is not None:
        payload["includes"] = {"users": users}
    return payload


@pytest.fixture
def fake_api():
    return FakeXApi()


@pytest.fixture
def client_factory():
    def build(api):
        return XSearchClient("test-token", transport=api, sleep=lambda _: None)

    return build


@pytest.fixture(scope="session")
def scorer():
    return SentimentScorer()
