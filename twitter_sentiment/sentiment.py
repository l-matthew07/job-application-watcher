"""Sentiment scoring tuned for posts about companies.

Built on VADER (``vaderSentiment``), which is the right base for this: it was
built for social text, so it already handles emoji, ALL-CAPS emphasis,
punctuation intensity ("great!!!"), degree modifiers ("really great") and
negation ("not great") without any training step.

What VADER does *not* know is the vocabulary people use about companies. Its
7.5k-word lexicon has ``lawsuit`` and ``defective`` but not ``layoffs``,
``outage``, ``bricked`` or ``buggy`` — all of which are strongly polar in this
domain and would otherwise score a flat 0.0. :data:`DOMAIN_LEXICON` fills that
gap, and :data:`DOMAIN_PHRASES` covers multi-word terms, which VADER cannot
represent at all because it scores whitespace-separated tokens.

Known limitations, in rough order of how much they'll bite:

* **Sarcasm.** "great, another outage" scores positive on ``great``. No
  lexicon method fixes this.
* **English only.** VADER's lexicon is English; other languages score ~0.0,
  which is indistinguishable from genuine neutrality. :class:`SentimentScorer`
  therefore refuses unsupported languages outright rather than returning a
  misleading zero — see :meth:`SentimentScorer.score_post`.
* **Polarity is not aboutness.** A post can be angry at a replier while
  mentioning a company neutrally. Relevance filtering in ``text.py`` narrows
  this but does not eliminate it.
"""

from __future__ import annotations

import re

from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

from .models import Post, Sentiment
from .text import clean_for_sentiment

# Single tokens VADER's lexicon is missing, on its -4.0..+4.0 valence scale.
# Only words absent from (or badly skewed in) the stock lexicon belong here;
# anything VADER already scores sensibly is deliberately left alone.
DOMAIN_LEXICON: dict[str, float] = {
    # Corporate / financial trouble
    "layoff": -2.6,
    "layoffs": -2.8,
    "downsizing": -2.0,
    "antitrust": -1.5,
    "subpoena": -1.8,
    "probe": -1.2,
    "delisted": -2.2,
    "downgrade": -1.6,
    "downgraded": -1.6,
    "rugpull": -3.2,
    "insolvent": -3.0,
    # Product / service failure. "recall" is ambiguous in general English
    # (memory recall) but in company-mention text it is near-always a product
    # recall, so it carries a moderate negative rather than a strong one.
    "recall": -1.5,
    "recalled": -1.7,
    "outage": -2.4,
    "downtime": -2.0,
    "bricked": -2.8,
    "unusable": -2.6,
    "buggy": -2.2,
    "glitchy": -2.0,
    "laggy": -1.8,
    "janky": -1.8,
    "crashy": -2.0,
    "throttled": -1.6,
    "bloated": -1.5,
    "overpriced": -2.0,
    "paywall": -1.4,
    "paywalled": -1.6,
    "enshittification": -3.0,
    "chargeback": -1.0,
    "backordered": -1.0,
    "sketchy": -1.8,
    # Praise
    "seamless": 2.2,
    "buttery": 1.8,
    "underrated": 1.6,
    "polished": 1.8,
    "shipped": 1.0,
    "goated": 3.0,
    "banger": 2.6,
    "slaps": 2.2,
    "nails": 1.4,
    "delightful": 2.6,
}

# Multi-word terms. VADER scores whitespace tokens, so these are collapsed to
# a single underscore token before analysis and registered under that token.
DOMAIN_PHRASES: dict[str, float] = {
    "data breach": -3.0,
    "security breach": -3.0,
    "class action": -2.2,
    "price hike": -2.0,
    "price gouging": -2.8,
    "dark pattern": -2.2,
    "vendor lock": -1.6,
    "planned obsolescence": -2.4,
    "customer service nightmare": -3.2,
    "worth every penny": 3.0,
    "game changer": 2.6,
    "just works": 2.4,
    "best in class": 2.8,
    "night and day": 1.8,
}

DEFAULT_SUPPORTED_LANGS = ("en",)


def _phrase_token(phrase: str) -> str:
    return phrase.replace(" ", "_")


def _phrase_pattern(phrase: str) -> re.Pattern[str]:
    # Tolerate any run of whitespace between the words of a phrase.
    return re.compile(
        r"(?<!\w)" + r"\s+".join(re.escape(w) for w in phrase.split()) + r"(?!\w)",
        re.IGNORECASE,
    )


class SentimentScorer:
    """Scores text, with the domain lexicon merged into stock VADER.

    The analyzer is stateful (it owns the merged lexicon), so build one and
    reuse it across a run rather than constructing per post — loading VADER's
    lexicon is the expensive part.
    """

    def __init__(
        self,
        extra_lexicon: dict[str, float] | None = None,
        extra_phrases: dict[str, float] | None = None,
        supported_langs=DEFAULT_SUPPORTED_LANGS,
    ) -> None:
        self._analyzer = SentimentIntensityAnalyzer()
        self.supported_langs = tuple(supported_langs)

        self._analyzer.lexicon.update(DOMAIN_LEXICON)
        if extra_lexicon:
            self._analyzer.lexicon.update(extra_lexicon)

        phrases = dict(DOMAIN_PHRASES)
        if extra_phrases:
            phrases.update(extra_phrases)
        # Longest first, so "security breach" wins over any shorter overlap.
        self._phrases = sorted(
            ((_phrase_pattern(p), _phrase_token(p)) for p in phrases),
            key=lambda item: -len(item[1]),
        )
        self._analyzer.lexicon.update(
            {_phrase_token(p): v for p, v in phrases.items()}
        )

    def _collapse_phrases(self, text: str) -> str:
        for pattern, token in self._phrases:
            text = pattern.sub(token, text)
        return text

    def score(self, text: str) -> Sentiment:
        """Score already-cleaned or raw text. Empty text scores neutral."""
        prepared = self._collapse_phrases(clean_for_sentiment(text))
        if not prepared:
            return Sentiment()
        raw = self._analyzer.polarity_scores(prepared)
        return Sentiment(
            compound=raw["compound"],
            positive=raw["pos"],
            neutral=raw["neu"],
            negative=raw["neg"],
        )

    def supports_lang(self, lang: str) -> bool:
        """An absent/``und`` language tag is given the benefit of the doubt."""
        if not lang or lang in ("und", "unknown"):
            return True
        return lang in self.supported_langs

    def score_post(self, post: Post) -> Sentiment | None:
        """Score a post, or return ``None`` if its language is unsupported.

        ``None`` rather than a zeroed :class:`Sentiment` on purpose: a flat 0.0
        is indistinguishable from genuine neutrality, and silently folding
        unscoreable posts into a company's mean as "neutral" would bias every
        rollup toward zero.
        """
        if not self.supports_lang(post.lang):
            return None
        return self.score(post.text)
