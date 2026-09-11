"""Roll scored posts up into a per-company sentiment picture."""

from __future__ import annotations

from typing import Iterable, Sequence

from .models import CompanySentiment, ScoredPost

DEFAULT_EXAMPLES = 3


def _rank(post: ScoredPost) -> tuple[float, int]:
    """Sort key: how strong the opinion is, then how far it travelled."""
    return (abs(post.sentiment.compound), post.post.metrics.engagement)


def summarize(
    company: str,
    scored: Iterable[ScoredPost],
    examples: int = DEFAULT_EXAMPLES,
) -> CompanySentiment:
    """Aggregate one company's scored posts.

    Two means are reported because they answer different questions. The plain
    mean is "what does a random person posting about this company think"; the
    engagement-weighted mean is "what did the timeline actually see". They come
    apart exactly when one loud post dominates, which is the case worth noticing.
    """
    posts: Sequence[ScoredPost] = list(scored)
    if not posts:
        return CompanySentiment(company=company)

    counts = {"positive": 0, "neutral": 0, "negative": 0}
    compound_total = 0.0
    weighted_total = 0.0
    weight_total = 0.0
    engagement = 0

    for item in posts:
        counts[item.sentiment.label] += 1
        compound_total += item.sentiment.compound
        weight = item.weight
        weighted_total += item.sentiment.compound * weight
        weight_total += weight
        engagement += item.post.metrics.engagement

    positives = sorted(
        (p for p in posts if p.sentiment.label == "positive"), key=_rank, reverse=True
    )
    negatives = sorted(
        (p for p in posts if p.sentiment.label == "negative"), key=_rank, reverse=True
    )

    return CompanySentiment(
        company=company,
        total=len(posts),
        positive=counts["positive"],
        neutral=counts["neutral"],
        negative=counts["negative"],
        mean_compound=compound_total / len(posts),
        weighted_compound=weighted_total / weight_total if weight_total else 0.0,
        total_engagement=engagement,
        top_positive=tuple(positives[:examples]),
        top_negative=tuple(negatives[:examples]),
    )
