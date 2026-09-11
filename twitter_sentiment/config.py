"""Per-company watch configuration and X search-query construction.

A company is rarely one keyword. "Apple" is a fruit, "Rivian" is unambiguous,
and "$TSLA" only ever means the stock — so each company carries a list of terms
to match, a list of terms that disqualify a hit, and the knobs that go into the
search query itself.

The query string built here is what the X API sees; the ``terms``/
``exclude_terms`` lists are *also* re-checked locally in the pipeline, because
the search endpoint matches tokenized text and will return hits that don't
survive a word-boundary check.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

import yaml

# The recent-search endpoint caps query length by access tier: 512 characters
# on Basic, higher on Pro/Enterprise. Default to the tightest so a config that
# validates here works everywhere.
MAX_QUERY_LENGTH = 512
MAX_RESULTS_FLOOR = 10
MAX_RESULTS_CEILING = 100

# A bare term needs quoting in an X query if it contains whitespace; operators
# like "$TSLA" and "#tesla" must NOT be quoted or they stop being operators.
_NEEDS_QUOTING_RE = re.compile(r"\s")


class ConfigError(ValueError):
    """Raised for a config file that parses but doesn't describe a valid run."""


def quote_term(term: str) -> str:
    term = term.strip()
    if not term:
        raise ConfigError("empty search term")
    if term.startswith('"') and term.endswith('"'):
        return term
    return f'"{term}"' if _NEEDS_QUOTING_RE.search(term) else term


@dataclass(frozen=True)
class CompanyConfig:
    """One watched company."""

    name: str
    terms: tuple[str, ...]
    exclude_terms: tuple[str, ...] = ()
    lang: str = "en"
    exclude_retweets: bool = True
    exclude_replies: bool = False
    max_results: int = MAX_RESULTS_CEILING
    max_pages: int = 3
    extra_query: str = ""

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ConfigError("company is missing a name")
        if not self.terms:
            raise ConfigError(f"{self.name}: at least one search term is required")
        if not MAX_RESULTS_FLOOR <= self.max_results <= MAX_RESULTS_CEILING:
            raise ConfigError(
                f"{self.name}: max_results must be "
                f"{MAX_RESULTS_FLOOR}-{MAX_RESULTS_CEILING}, got {self.max_results}"
            )
        if self.max_pages < 1:
            raise ConfigError(f"{self.name}: max_pages must be >= 1")

    def build_query(self, max_length: int = MAX_QUERY_LENGTH) -> str:
        """Render the X API v2 search query for this company.

        Raises :class:`ConfigError` rather than letting the API reject the
        request — a 400 halfway through a run is a worse failure mode than a
        validation error at startup.
        """
        clause = " OR ".join(quote_term(t) for t in self.terms)
        parts = [f"({clause})" if len(self.terms) > 1 else clause]
        parts += [f"-{quote_term(t)}" for t in self.exclude_terms]
        if self.exclude_retweets:
            parts.append("-is:retweet")
        if self.exclude_replies:
            parts.append("-is:reply")
        if self.lang:
            parts.append(f"lang:{self.lang}")
        if self.extra_query.strip():
            parts.append(self.extra_query.strip())

        query = " ".join(parts)
        if len(query) > max_length:
            raise ConfigError(
                f"{self.name}: query is {len(query)} chars, over the "
                f"{max_length}-char limit — trim terms or split the company"
            )
        return query


@dataclass(frozen=True)
class ScrapeConfig:
    """A whole run: the companies plus run-wide settings."""

    companies: tuple[CompanyConfig, ...] = ()
    output_path: str = "sentiment.jsonl"
    state_path: str = "sentiment_state.json"
    extra_lexicon: dict = field(default_factory=dict)
    extra_phrases: dict = field(default_factory=dict)

    def company(self, name: str) -> CompanyConfig:
        for company in self.companies:
            if company.name.lower() == name.lower():
                return company
        raise ConfigError(f"no company named {name!r} in config")


def _company_from_dict(payload: dict, defaults: dict) -> CompanyConfig:
    if not isinstance(payload, dict):
        raise ConfigError(f"each company must be a mapping, got {type(payload).__name__}")

    merged = {**defaults, **payload}
    name = str(merged.get("name", "")).strip()

    # A company with no explicit terms watches its own name.
    terms = merged.get("terms") or ([name] if name else [])
    if isinstance(terms, str):
        terms = [terms]
    excludes = merged.get("exclude_terms") or []
    if isinstance(excludes, str):
        excludes = [excludes]

    known = {
        "lang", "exclude_retweets", "exclude_replies",
        "max_results", "max_pages", "extra_query",
    }
    unknown = set(payload) - known - {"name", "terms", "exclude_terms"}
    if unknown:
        raise ConfigError(f"{name or '<unnamed>'}: unknown keys {sorted(unknown)}")

    return CompanyConfig(
        name=name,
        terms=tuple(str(t) for t in terms),
        exclude_terms=tuple(str(t) for t in excludes),
        lang=str(merged.get("lang", "en")),
        exclude_retweets=bool(merged.get("exclude_retweets", True)),
        exclude_replies=bool(merged.get("exclude_replies", False)),
        max_results=int(merged.get("max_results", MAX_RESULTS_CEILING)),
        max_pages=int(merged.get("max_pages", 3)),
        extra_query=str(merged.get("extra_query", "")),
    )


def load_config(path: str | Path) -> ScrapeConfig:
    """Load and validate a companies YAML file."""
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top level must be a mapping")

    defaults = raw.get("defaults") or {}
    if not isinstance(defaults, dict):
        raise ConfigError(f"{path}: 'defaults' must be a mapping")

    entries = raw.get("companies")
    if not entries:
        raise ConfigError(f"{path}: no companies configured")

    companies = tuple(_company_from_dict(e, defaults) for e in entries)
    seen = set()
    for company in companies:
        key = company.name.lower()
        if key in seen:
            raise ConfigError(f"duplicate company: {company.name}")
        seen.add(key)
        company.build_query()  # fail fast on an over-length query

    return ScrapeConfig(
        companies=companies,
        output_path=str(raw.get("output_path", "sentiment.jsonl")),
        state_path=str(raw.get("state_path", "sentiment_state.json")),
        extra_lexicon=dict(raw.get("extra_lexicon") or {}),
        extra_phrases=dict(raw.get("extra_phrases") or {}),
    )


def config_from_terms(name: str, terms, **overrides) -> ScrapeConfig:
    """Build a one-company config from CLI args, skipping the YAML file."""
    company = CompanyConfig(name=name, terms=tuple(terms) or (name,))
    if overrides:
        company = replace(company, **overrides)
    company.build_query()
    return ScrapeConfig(companies=(company,))


def bearer_token(env: dict | None = None) -> str:
    """Read the app-only bearer token, failing with an actionable message."""
    env = os.environ if env is None else env
    token = (env.get("X_BEARER_TOKEN") or env.get("TWITTER_BEARER_TOKEN") or "").strip()
    if not token:
        raise ConfigError(
            "X_BEARER_TOKEN is not set — create an app-only bearer token in the "
            "X developer portal and put it in .env (see .env.example)"
        )
    return token
