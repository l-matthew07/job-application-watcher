"""Text cleanup, relevance matching, and spam heuristics.

Two jobs, and they want different text:

* **Sentiment** wants the human-written words only — URLs, @handles and the
  ``RT @acct:`` prefix carry no polarity and VADER will happily mis-tokenize
  them. :func:`clean_for_sentiment` strips those and unpacks hashtags.
* **Relevance** wants the raw text, because a company can legitimately be
  mentioned only in a hashtag or a cashtag.

Search queries are blunt: a query for ``Apple`` catches the fruit, and a query
for any consumer brand catches a flood of giveaway spam. Filtering here is what
keeps the sentiment numbers meaning anything.
"""

from __future__ import annotations

import hashlib
import html
import re
import unicodedata

from .models import Author, Post

URL_RE = re.compile(r"https?://\S+|\bt\.co/\S+", re.IGNORECASE)
MENTION_RE = re.compile(r"(?<![\w/])@(\w{1,15})\b")
HASHTAG_RE = re.compile(r"#(\w+)")
RT_PREFIX_RE = re.compile(r"^RT\s+@\w{1,15}:\s*")
CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
WHITESPACE_RE = re.compile(r"\s+")
NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")

# Promo/engagement-farming markers. A post needs several of these before it is
# dropped (see SPAM_HIT_THRESHOLD) so ordinary launch excitement survives.
SPAM_MARKERS = (
    "giveaway",
    "airdrop",
    "free crypto",
    "promo code",
    "discount code",
    "use my code",
    "retweet to win",
    "rt to win",
    "rt & follow",
    "follow & rt",
    "like and retweet",
    "tag 3 friends",
    "dm for promo",
    "click the link in bio",
    "limited time offer",
    "buy now",
)
SPAM_HIT_THRESHOLD = 1
MAX_HASHTAGS = 5
MAX_MENTIONS = 5
MIN_WORDS = 3
NEW_ACCOUNT_DAYS = 14


def strip_accents(value: str) -> str:
    """Fold accented characters so ``Nestlé`` matches a plain-ASCII term."""
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def split_hashtag(tag: str) -> str:
    """``#ShipItFast`` -> ``Ship It Fast`` so the words reach the scorer."""
    return CAMEL_BOUNDARY_RE.sub(" ", tag.replace("_", " ")).strip()


def clean_for_sentiment(text: str) -> str:
    """Reduce a post to the words a sentiment model should actually read."""
    if not text:
        return ""
    cleaned = html.unescape(text)
    cleaned = RT_PREFIX_RE.sub("", cleaned)
    cleaned = URL_RE.sub(" ", cleaned)
    cleaned = MENTION_RE.sub(" ", cleaned)
    cleaned = HASHTAG_RE.sub(lambda m: " " + split_hashtag(m.group(1)), cleaned)
    return WHITESPACE_RE.sub(" ", cleaned).strip()


# 8 bytes of BLAKE2b. Dedupe keys are stored in bulk — a few thousand per
# company — so they are hashed rather than kept as normalized text: the raw
# form averages ~200 chars, which puts a 5k ring past DynamoDB's 400KB item
# limit on its own. At 16 hex chars a collision needs ~5 billion distinct
# posts per company to become likely, and the cost of one is a single dropped
# post, not corruption.
DEDUPE_DIGEST_BYTES = 8


def dedupe_key(text: str) -> str:
    """Fingerprint for near-identical copypasta (same words, different links).

    Case, punctuation, URLs, handles and whitespace are discarded before
    hashing, so the two halves of a quote-tweet chain collapse onto one key.
    Empty text has no fingerprint and returns "" rather than the hash of the
    empty string — callers treat "" as "not dedupable".
    """
    normalized = NON_ALNUM_RE.sub(
        "", strip_accents(clean_for_sentiment(text)).lower()
    )
    if not normalized:
        return ""
    return hashlib.blake2b(
        normalized.encode("utf-8"), digest_size=DEDUPE_DIGEST_BYTES
    ).hexdigest()


def _term_pattern(term: str) -> re.Pattern[str]:
    """Word-boundary matcher for a term, tolerant of ``#``/``$`` prefixes.

    ``\\b`` does not fire before ``#``, so ``#tesla`` would miss a ``\\btesla\\b``
    pattern; the optional sigil and lookarounds handle that without matching
    ``teslas`` inside another word.
    """
    escaped = re.escape(strip_accents(term).lower())
    return re.compile(rf"(?<![\w]){escaped}(?![\w])", re.IGNORECASE)


_PATTERN_CACHE: dict[str, re.Pattern[str]] = {}


def term_pattern(term: str) -> re.Pattern[str]:
    key = strip_accents(term).lower()
    pattern = _PATTERN_CACHE.get(key)
    if pattern is None:
        pattern = _term_pattern(term)
        _PATTERN_CACHE[key] = pattern
    return pattern


def find_terms(text: str, terms) -> tuple[str, ...]:
    """Return the subset of ``terms`` present in ``text``, order preserved."""
    haystack = strip_accents(html.unescape(text or "")).lower()
    return tuple(t for t in terms if term_pattern(t).search(haystack))


def is_low_quality(post: Post, author: Author | None = None, now=None) -> str | None:
    """Return a reason string when a post should be dropped, else ``None``.

    Returning the reason (rather than a bool) makes the drop counts in the CLI
    summary explainable — you can see *why* a company's volume collapsed.
    """
    body = clean_for_sentiment(post.text)

    if len(body.split()) < MIN_WORDS:
        return "too_short"

    lowered = body.lower()
    hits = sum(1 for marker in SPAM_MARKERS if marker in lowered)
    if hits >= SPAM_HIT_THRESHOLD:
        return "promo_spam"

    raw = html.unescape(post.text or "")
    if len(HASHTAG_RE.findall(raw)) > MAX_HASHTAGS:
        return "hashtag_stuffing"
    if len(MENTION_RE.findall(raw)) > MAX_MENTIONS:
        return "mention_stuffing"

    if author is not None:
        age = author.age_days(now)
        if age is not None and age < NEW_ACCOUNT_DAYS and author.followers < 50:
            return "throwaway_account"

    return None
