import pytest

from twitter_sentiment.aggregate import summarize
from twitter_sentiment.models import Metrics, Post, ScoredPost, Sentiment


def scored(compound, likes=0, text="post", post_id="1"):
    return ScoredPost(
        post=Post(id=post_id, text=text, metrics=Metrics(likes=likes)),
        author=None,
        sentiment=Sentiment(compound=compound),
        company="Tesla",
    )


def test_empty_input_returns_an_empty_rollup():
    summary = summarize("Tesla", [])

    assert summary.total == 0
    assert summary.mean_compound == 0.0
    assert summary.weighted_compound == 0.0
    assert summary.top_positive == ()


def test_counts_and_mean():
    summary = summarize("Tesla", [scored(0.8), scored(-0.6), scored(0.0)])

    assert (summary.total, summary.positive, summary.negative, summary.neutral) == (3, 1, 1, 1)
    assert summary.mean_compound == pytest.approx(0.2 / 3)


def test_engagement_is_summed():
    summary = summarize("Tesla", [scored(0.5, likes=10), scored(0.5, likes=5)])

    assert summary.total_engagement == 15


def test_weighted_mean_leans_toward_the_high_engagement_post():
    summary = summarize("Tesla", [scored(0.9, likes=50_000), scored(-0.9, likes=0)])

    assert summary.mean_compound == pytest.approx(0.0)
    assert summary.weighted_compound > 0.4


def test_weighted_mean_equals_plain_mean_without_engagement():
    posts = [scored(0.8), scored(-0.4), scored(0.1)]
    summary = summarize("Tesla", posts)

    assert summary.weighted_compound == pytest.approx(summary.mean_compound)


def test_top_examples_are_ranked_by_strength_then_reach():
    summary = summarize("Tesla", [
        scored(0.3, text="mild", post_id="1"),
        scored(0.9, text="strong", post_id="2"),
        scored(0.9, likes=100, text="strong and viral", post_id="3"),
        scored(-0.95, text="worst", post_id="4"),
        scored(-0.2, text="meh", post_id="5"),
    ])

    assert [p.post.text for p in summary.top_positive] == [
        "strong and viral", "strong", "mild",
    ]
    assert [p.post.text for p in summary.top_negative] == ["worst", "meh"]


def test_examples_are_capped():
    summary = summarize("Tesla", [scored(0.5, post_id=str(i)) for i in range(10)], examples=2)

    assert len(summary.top_positive) == 2


def test_neutral_posts_never_appear_as_examples():
    summary = summarize("Tesla", [scored(0.0), scored(0.01)])

    assert summary.top_positive == () and summary.top_negative == ()
