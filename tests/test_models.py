import datetime as dt

import pytest

from twitter_sentiment.models import (
    Author,
    CompanySentiment,
    Metrics,
    Post,
    ScoredPost,
    Sentiment,
    posts_from_response,
)

UTC = dt.timezone.utc


def test_post_from_api_parses_full_payload():
    post = Post.from_api({
        "id": "1800000000000000001",
        "text": "Shipped a thing.",
        "author_id": "42",
        "created_at": "2026-09-10T12:00:00.000Z",
        "lang": "en",
        "conversation_id": "1800000000000000000",
        "referenced_tweets": [{"type": "replied_to", "id": "1799"}],
        "public_metrics": {
            "like_count": 10,
            "retweet_count": 3,
            "reply_count": 2,
            "quote_count": 1,
            "impression_count": 900,
        },
    })

    assert post.id == "1800000000000000001"
    assert post.created_at == dt.datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    assert post.is_reply and not post.is_retweet
    assert post.metrics.engagement == 16


def test_post_from_api_tolerates_minimal_payload():
    post = Post.from_api({"id": "1", "text": "hi"})

    assert post.created_at is None
    assert post.referenced_types == ()
    assert post.metrics == Metrics()
    assert post.metrics.engagement == 0


def test_post_from_api_ignores_unparseable_timestamp():
    assert Post.from_api({"id": "1", "text": "x", "created_at": "nope"}).created_at is None


def test_post_url_falls_back_when_username_unknown():
    post = Post.from_api({"id": "99", "text": "x"})

    assert post.url("matthew") == "https://x.com/matthew/status/99"
    assert post.url("") == "https://x.com/i/status/99"


def test_author_age_days():
    author = Author.from_api({
        "id": "42",
        "username": "acct",
        "created_at": "2026-09-01T00:00:00.000Z",
        "public_metrics": {"followers_count": 12, "tweet_count": 3},
    })

    assert author.followers == 12
    assert author.age_days(dt.datetime(2026, 9, 11, tzinfo=UTC)) == pytest.approx(10.0)


def test_author_age_days_is_none_without_created_at():
    assert Author.from_api({"id": "42"}).age_days() is None


@pytest.mark.parametrize(
    "compound,label",
    [
        (0.9, "positive"),
        (0.05, "positive"),
        (0.049, "neutral"),
        (0.0, "neutral"),
        (-0.049, "neutral"),
        (-0.05, "negative"),
        (-0.8, "negative"),
    ],
)
def test_sentiment_label_boundaries(compound, label):
    assert Sentiment(compound=compound).label == label


def test_scored_post_weight_is_log_damped():
    quiet = ScoredPost(Post("1", "a"), None, Sentiment())
    viral = ScoredPost(
        Post("2", "b", metrics=Metrics(likes=100000)), None, Sentiment()
    )

    assert quiet.weight == pytest.approx(1.0)
    # 100k likes is worth ~12x a silent post, not 100,000x.
    assert 10 < viral.weight < 14


def test_scored_post_to_record_is_json_shaped():
    post = Post.from_api({
        "id": "7",
        "text": "great launch",
        "author_id": "42",
        "created_at": "2026-09-10T12:00:00.000Z",
        "public_metrics": {"like_count": 4},
    })
    author = Author(id="42", username="matthew")
    record = ScoredPost(
        post, author, Sentiment(compound=0.7), company="Tesla",
        matched_terms=("tesla",),
    ).to_record()

    assert record["company"] == "Tesla"
    assert record["label"] == "positive"
    assert record["url"] == "https://x.com/matthew/status/7"
    assert record["created_at"] == "2026-09-10T12:00:00+00:00"
    assert record["metrics"]["likes"] == 4
    assert record["matched_terms"] == ["tesla"]


def test_company_sentiment_net_and_summary():
    rollup = CompanySentiment(
        company="Tesla", total=10, positive=6, neutral=1, negative=3,
        mean_compound=0.25, weighted_compound=0.31,
    )

    assert rollup.net_sentiment == pytest.approx(0.3)
    assert "Tesla: 10 posts" in rollup.summary()
    assert "net +0.30" in rollup.summary()


def test_company_sentiment_empty_does_not_divide_by_zero():
    empty = CompanySentiment(company="Nobody")

    assert empty.net_sentiment == 0.0
    assert empty.summary() == "Nobody: no posts matched"


def test_posts_from_response_indexes_authors():
    posts, authors = posts_from_response({
        "data": [
            {"id": "1", "text": "a", "author_id": "42"},
            {"id": "2", "text": "b", "author_id": "43"},
        ],
        "includes": {"users": [
            {"id": "42", "username": "alice"},
            {"id": "43", "username": "bob"},
        ]},
        "meta": {"result_count": 2},
    })

    assert [p.id for p in posts] == ["1", "2"]
    assert authors["42"].username == "alice"
    assert authors[posts[1].author_id].username == "bob"


def test_posts_from_response_handles_empty_result():
    posts, authors = posts_from_response({"meta": {"result_count": 0}})

    assert posts == []
    assert authors == {}
