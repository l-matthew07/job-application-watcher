"""The scrape pipeline: search -> filter -> score -> aggregate.

Filtering runs before scoring and in cost order, cheapest disqualifier first.
The drop reasons are counted and reported, because the most common failure mode
of a scraper like this is silent: a query quietly stops matching, volume goes to
zero, and the sentiment number looks calm rather than broken.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .aggregate import summarize
from .client import XSearchClient
from .config import CompanyConfig
from .models import Author, CompanySentiment, ScoredPost, posts_from_response
from .sentiment import SentimentScorer
from .store import StateStore, id_max
from .text import dedupe_key, find_terms, is_low_quality


@dataclass
class CompanyRun:
    """Everything one company's scrape produced."""

    company: str
    summary: CompanySentiment
    scored: list[ScoredPost] = field(default_factory=list)
    drops: dict[str, int] = field(default_factory=dict)
    fetched: int = 0
    newest_id: str = ""

    @property
    def kept(self) -> int:
        return len(self.scored)

    def drop_report(self) -> str:
        if not self.drops:
            return "none dropped"
        ordered = sorted(self.drops.items(), key=lambda kv: -kv[1])
        return ", ".join(f"{reason}={count}" for reason, count in ordered)


def run_company(
    client: XSearchClient,
    scorer: SentimentScorer,
    company: CompanyConfig,
    state: StateStore | None = None,
    now=None,
) -> CompanyRun:
    """Scrape, filter and score one company. Never raises on empty results."""
    query = company.build_query()
    since_id = state.since_id(company.name) if state else None

    scored: list[ScoredPost] = []
    drops: dict[str, int] = {}
    fetched = 0
    newest_id = ""
    # Within-run dedupe, so a company with no persistent state still collapses
    # copypasta appearing twice in the same scrape.
    seen_this_run: set[str] = set()

    def drop(reason: str) -> None:
        drops[reason] = drops.get(reason, 0) + 1

    pages = client.search_recent(
        query,
        max_results=company.max_results,
        max_pages=company.max_pages,
        since_id=since_id,
    )

    for payload in pages:
        posts, authors = posts_from_response(payload)
        meta = payload.get("meta") or {}
        newest_id = id_max(newest_id, str(meta.get("newest_id") or ""))

        for post in posts:
            fetched += 1
            newest_id = id_max(newest_id, post.id)
            author: Author | None = authors.get(post.author_id)

            matched = find_terms(post.text, company.terms)
            if not matched:
                drop("no_term_match")
                continue
            if company.exclude_terms and find_terms(post.text, company.exclude_terms):
                drop("excluded_term")
                continue

            reason = is_low_quality(post, author, now)
            if reason:
                drop(reason)
                continue

            key = dedupe_key(post.text)
            if key in seen_this_run or (state and state.is_duplicate(company.name, key)):
                drop("duplicate")
                continue

            sentiment = scorer.score_post(post)
            if sentiment is None:
                drop("unsupported_lang")
                continue

            seen_this_run.add(key)
            if state:
                state.remember(company.name, key)
            scored.append(
                ScoredPost(
                    post=post, author=author, sentiment=sentiment,
                    company=company.name, matched_terms=matched,
                )
            )

    if state:
        state.advance(company.name, newest_id)

    return CompanyRun(
        company=company.name,
        summary=summarize(company.name, scored),
        scored=scored,
        drops=drops,
        fetched=fetched,
        newest_id=newest_id,
    )
