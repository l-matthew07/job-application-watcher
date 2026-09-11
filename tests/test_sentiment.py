import pytest

from twitter_sentiment.models import Post
from twitter_sentiment.sentiment import DOMAIN_LEXICON, SentimentScorer


@pytest.fixture(scope="module")
def scorer():
    return SentimentScorer()


def test_clear_praise_scores_positive(scorer):
    assert scorer.score("Tesla's new update is fantastic, I love it").label == "positive"


def test_clear_complaint_scores_negative(scorer):
    assert scorer.score("Absolutely terrible experience, worst support ever").label == "negative"


def test_factual_statement_scores_neutral(scorer):
    assert scorer.score("Tesla reported Q3 deliveries this morning").label == "neutral"


def test_vader_negation_still_works(scorer):
    positive = scorer.score("the rollout is good").compound
    negated = scorer.score("the rollout is not good").compound

    assert positive > 0 and negated < 0


def test_vader_intensity_and_caps_still_work(scorer):
    plain = scorer.score("the launch is good").compound
    loud = scorer.score("the launch is GREAT!!!").compound

    assert loud > plain


@pytest.mark.parametrize(
    "text",
    [
        "another round of layoffs at the company",
        "third outage this week",
        "the update bricked my device",
        "app has been buggy since the redesign",
        "completely overpriced for what you get",
    ],
)
def test_domain_negatives_are_detected(scorer, text):
    assert scorer.score(text).label == "negative", text


@pytest.mark.parametrize(
    "text",
    [
        "the handoff is seamless across devices",
        "genuinely underrated hardware",
        "that keynote was a banger",
    ],
)
def test_domain_positives_are_detected(scorer, text):
    assert scorer.score(text).label == "positive", text


def test_domain_terms_would_otherwise_be_invisible():
    """Guards the premise of DOMAIN_LEXICON: stock VADER scores these flat."""
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

    stock = SentimentIntensityAnalyzer()
    tuned = SentimentScorer()

    assert stock.polarity_scores("layoffs announced today")["compound"] == 0.0
    assert tuned.score("layoffs announced today").compound < 0


def test_multiword_phrases_are_scored(scorer):
    # VADER scores whitespace tokens, so "data breach" only works via collapsing.
    assert scorer.score("they disclosed a data breach").label == "negative"
    assert scorer.score("honestly worth every penny").label == "positive"


def test_phrase_matching_tolerates_extra_whitespace(scorer):
    assert scorer.score("they disclosed a data   breach").label == "negative"


def test_longer_phrase_wins_over_shorter_overlap(scorer):
    assert scorer.score("a security breach was confirmed").compound < 0


def test_phrase_is_not_matched_inside_a_word(scorer):
    assert scorer.score("the metadata breaching module compiled").label == "neutral"


def test_urls_and_handles_do_not_leak_polarity(scorer):
    bare = scorer.score("the update is fine")
    noisy = scorer.score("RT @gooduser: the update is fine https://t.co/greatlink")

    assert bare == noisy


def test_hashtag_words_reach_the_scorer(scorer):
    assert scorer.score("shipping this #AbsolutelyBrilliant").label == "positive"


def test_emoji_are_scored(scorer):
    assert scorer.score("the new model 😍😍").label == "positive"


def test_empty_and_url_only_text_is_neutral(scorer):
    assert scorer.score("").compound == 0.0
    assert scorer.score("https://t.co/abc").compound == 0.0


def test_extra_lexicon_overrides_defaults(scorer):
    custom = SentimentScorer(extra_lexicon={"recall": -3.9})
    text = "a recall was issued"

    assert custom.score(text).compound < scorer.score(text).compound


def test_extra_phrases_are_registered(scorer):
    # "latency" and "cadence" carry no stock VADER valence on their own, so any
    # negative signal here has to come from the registered phrase.
    custom = SentimentScorer(extra_phrases={"latency cadence": -3.0})
    text = "reports of a latency cadence"

    assert scorer.score(text).label == "neutral"
    assert custom.score(text).label == "negative"


def test_score_post_skips_unsupported_language(scorer):
    assert scorer.score_post(Post(id="1", text="c'est vraiment terrible", lang="fr")) is None


def test_score_post_scores_english(scorer):
    result = scorer.score_post(Post(id="1", text="this is wonderful", lang="en"))

    assert result is not None and result.label == "positive"


@pytest.mark.parametrize("lang", ["", "und", "unknown"])
def test_score_post_allows_unknown_language(scorer, lang):
    assert scorer.score_post(Post(id="1", text="this is wonderful", lang=lang)) is not None


def test_supported_langs_are_configurable():
    multi = SentimentScorer(supported_langs=("en", "fr"))

    assert multi.supports_lang("fr")
    assert not multi.supports_lang("de")


def test_domain_lexicon_values_are_in_vader_range():
    assert all(-4.0 <= v <= 4.0 for v in DOMAIN_LEXICON.values())
