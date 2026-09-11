"""Data model for scraped posts, their authors, and sentiment scores.

Everything here is a plain frozen dataclass so results are hashable, cheap to
compare in tests, and trivially serializable to the JSONL store.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field, asdict
from typing import Any, Iterable

# VADER's conventional cutoffs for bucketing a compound score.
POSITIVE_CUTOFF = 0.05
NEGATIVE_CUTOFF = -0.05


def _parse_time(value: Any) -> dt.datetime | None:
    """Parse an X API RFC 3339 timestamp (``2026-09-11T04:43:07.000Z``)."""
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)
    if not isinstance(value, str) or not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass(frozen=True)
class Author:
    """The account that wrote a post. Only the fields we filter on."""

    id: str
    username: str = ""
    name: str = ""
    verified: bool = False
    followers: int = 0
    following: int = 0
    tweet_count: int = 0
    created_at: dt.datetime | None = None

    @classmethod
    def from_api(cls, payload: dict) -> "Author":
        metrics = payload.get("public_metrics") or {}
        return cls(
            id=str(payload.get("id", "")),
            username=payload.get("username", ""),
            name=payload.get("name", ""),
            verified=bool(payload.get("verified", False)),
            followers=int(metrics.get("followers_count", 0)),
            following=int(metrics.get("following_count", 0)),
            tweet_count=int(metrics.get("tweet_count", 0)),
            created_at=_parse_time(payload.get("created_at")),
        )

    def age_days(self, now: dt.datetime | None = None) -> float | None:
        """Account age in days, or None when ``user.fields`` weren't requested."""
        if self.created_at is None:
            return None
        now = now or dt.datetime.now(dt.timezone.utc)
        return (now - self.created_at).total_seconds() / 86400


@dataclass(frozen=True)
class Metrics:
    """Public engagement counts on a post."""

    likes: int = 0
    retweets: int = 0
    replies: int = 0
    quotes: int = 0
    impressions: int = 0

    @classmethod
    def from_api(cls, payload: dict | None) -> "Metrics":
        payload = payload or {}
        return cls(
            likes=int(payload.get("like_count", 0)),
            retweets=int(payload.get("retweet_count", 0)),
            replies=int(payload.get("reply_count", 0)),
            quotes=int(payload.get("quote_count", 0)),
            impressions=int(payload.get("impression_count", 0)),
        )

    @property
    def engagement(self) -> int:
        """Total interactions — used to weight a post's sentiment."""
        return self.likes + self.retweets + self.replies + self.quotes


@dataclass(frozen=True)
class Post:
    """One public post returned by the search endpoint."""

    id: str
    text: str
    author_id: str = ""
    created_at: dt.datetime | None = None
    lang: str = ""
    conversation_id: str = ""
    referenced_types: tuple[str, ...] = ()
    metrics: Metrics = field(default_factory=Metrics)

    @classmethod
    def from_api(cls, payload: dict) -> "Post":
        refs = payload.get("referenced_tweets") or []
        return cls(
            id=str(payload.get("id", "")),
            text=payload.get("text", ""),
            author_id=str(payload.get("author_id", "")),
            created_at=_parse_time(payload.get("created_at")),
            lang=payload.get("lang", ""),
            conversation_id=str(payload.get("conversation_id", "")),
            referenced_types=tuple(
                r.get("type", "") for r in refs if isinstance(r, dict)
            ),
            metrics=Metrics.from_api(payload.get("public_metrics")),
        )

    @property
    def is_retweet(self) -> bool:
        return "retweeted" in self.referenced_types

    @property
    def is_reply(self) -> bool:
        return "replied_to" in self.referenced_types

    def url(self, username: str = "i") -> str:
        return f"https://x.com/{username or 'i'}/status/{self.id}"


@dataclass(frozen=True)
class Sentiment:
    """A VADER-style polarity breakdown. ``compound`` is the headline number."""

    compound: float = 0.0
    positive: float = 0.0
    neutral: float = 0.0
    negative: float = 0.0

    @property
    def label(self) -> str:
        if self.compound >= POSITIVE_CUTOFF:
            return "positive"
        if self.compound <= NEGATIVE_CUTOFF:
            return "negative"
        return "neutral"


@dataclass(frozen=True)
class ScoredPost:
    """A post that survived filtering, paired with its author and score."""

    post: Post
    author: Author | None
    sentiment: Sentiment
    company: str = ""
    matched_terms: tuple[str, ...] = ()

    @property
    def weight(self) -> float:
        """Engagement weight, log-damped so one viral post can't own the mean."""
        return 1.0 + math.log1p(self.post.metrics.engagement)

    def to_record(self) -> dict:
        """Flat dict for the JSONL store."""
        created = self.post.created_at
        return {
            "company": self.company,
            "id": self.post.id,
            "text": self.post.text,
            "created_at": created.isoformat() if created else None,
            "lang": self.post.lang,
            "author_id": self.post.author_id,
            "author_username": self.author.username if self.author else "",
            "url": self.post.url(self.author.username if self.author else ""),
            "metrics": asdict(self.post.metrics),
            "sentiment": asdict(self.sentiment),
            "label": self.sentiment.label,
            "matched_terms": list(self.matched_terms),
        }


@dataclass(frozen=True)
class CompanySentiment:
    """Rolled-up sentiment for one company over one scrape window."""

    company: str
    total: int = 0
    positive: int = 0
    neutral: int = 0
    negative: int = 0
    mean_compound: float = 0.0
    weighted_compound: float = 0.0
    total_engagement: int = 0
    top_positive: tuple[ScoredPost, ...] = ()
    top_negative: tuple[ScoredPost, ...] = ()

    @property
    def net_sentiment(self) -> float:
        """Share positive minus share negative, in [-1, 1]. 0.0 when empty."""
        if not self.total:
            return 0.0
        return (self.positive - self.negative) / self.total

    def summary(self) -> str:
        if not self.total:
            return f"{self.company}: no posts matched"
        return (
            f"{self.company}: {self.total} posts | "
            f"+{self.positive} ~{self.neutral} -{self.negative} | "
            f"net {self.net_sentiment:+.2f} | "
            f"mean {self.mean_compound:+.3f} "
            f"(engagement-weighted {self.weighted_compound:+.3f})"
        )


def posts_from_response(payload: dict) -> tuple[list[Post], dict[str, Author]]:
    """Split a search response into posts and an author_id -> Author index."""
    posts = [Post.from_api(item) for item in (payload.get("data") or [])]
    users: Iterable[dict] = (payload.get("includes") or {}).get("users") or []
    authors = {a.id: a for a in (Author.from_api(u) for u in users) if a.id}
    return posts, authors
