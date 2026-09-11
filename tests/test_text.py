import datetime as dt

import pytest

from twitter_sentiment.models import Author, Post
from twitter_sentiment.text import (
    clean_for_sentiment,
    dedupe_key,
    find_terms,
    is_low_quality,
    split_hashtag,
    strip_accents,
)

UTC = dt.timezone.utc


def post(text, **kwargs):
    return Post(id=kwargs.pop("id", "1"), text=text, **kwargs)


def test_clean_strips_urls_mentions_and_rt_prefix():
    raw = "RT @elonmusk: @tesla the new update is great https://t.co/abc123"

    assert clean_for_sentiment(raw) == "the new update is great"


def test_clean_unescapes_html_entities():
    assert clean_for_sentiment("Tesla &amp; Rivian &gt; the rest") == "Tesla & Rivian > the rest"


def test_clean_unpacks_hashtags_into_words():
    assert clean_for_sentiment("love the #ShipItFast culture") == "love the Ship It Fast culture"
    assert clean_for_sentiment("#physical_ai is here") == "physical ai is here"


def test_clean_preserves_emoji_for_the_scorer():
    # VADER reads emoji, so they must survive cleaning.
    assert "🔥" in clean_for_sentiment("this launch 🔥 https://t.co/x")


def test_clean_handles_empty_and_url_only_posts():
    assert clean_for_sentiment("") == ""
    assert clean_for_sentiment("https://t.co/abc") == ""


def test_email_like_text_is_not_treated_as_a_mention():
    assert "support" in clean_for_sentiment("email support@tesla.com about it")


@pytest.mark.parametrize(
    "tag,expected",
    [("ShipItFast", "Ship It Fast"), ("physical_ai", "physical ai"),
     ("AI", "AI"), ("Model3Launch", "Model3 Launch")],
)
def test_split_hashtag(tag, expected):
    assert split_hashtag(tag) == expected


def test_strip_accents():
    assert strip_accents("Nestlé Café") == "Nestle Cafe"


def test_dedupe_key_collapses_copypasta_variants():
    a = "Tesla's FSD is genuinely impressive now! https://t.co/aaa"
    b = "@someone Tesla's FSD is genuinely impressive now!!  https://t.co/bbb"

    assert dedupe_key(a) == dedupe_key(b)


def test_dedupe_key_separates_different_posts():
    assert dedupe_key("Tesla is great") != dedupe_key("Tesla is terrible")


def test_find_terms_is_word_boundary_aware():
    assert find_terms("I drive a Tesla daily", ["tesla"]) == ("tesla",)
    # "teslas" must not satisfy a "tesla" term via a naive substring match.
    assert find_terms("Teslamania is a fan site", ["tesla"]) == ()


def test_find_terms_matches_hashtags_and_cashtags():
    assert find_terms("bullish on $TSLA and #Tesla", ["tsla", "tesla"]) == ("tsla", "tesla")


def test_find_terms_is_accent_and_case_insensitive():
    assert find_terms("Nestlé recalled it", ["nestle"]) == ("nestle",)


def test_find_terms_returns_only_present_terms():
    assert find_terms("Rivian truck review", ["tesla", "rivian", "lucid"]) == ("rivian",)


def test_low_quality_flags_short_posts():
    assert is_low_quality(post("lol")) == "too_short"


def test_low_quality_flags_promo_spam():
    assert is_low_quality(post("Tesla GIVEAWAY! RT to win a free Model 3")) == "promo_spam"


def test_low_quality_flags_hashtag_stuffing():
    text = "Tesla stock #tesla #tsla #ev #stocks #invest #money #trading"

    assert is_low_quality(post(text)) == "hashtag_stuffing"


def test_low_quality_flags_mention_stuffing():
    text = "look at this @a @b @c @d @e @f Tesla thing that happened"

    assert is_low_quality(post(text)) == "mention_stuffing"


def test_low_quality_flags_new_low_follower_accounts():
    author = Author(
        id="1", username="bot", followers=2,
        created_at=dt.datetime(2026, 9, 9, tzinfo=UTC),
    )
    now = dt.datetime(2026, 9, 11, tzinfo=UTC)

    assert is_low_quality(post("Tesla is doing fine I guess"), author, now) == "throwaway_account"


def test_low_quality_keeps_new_accounts_with_real_followings():
    author = Author(
        id="1", username="newsdesk", followers=9000,
        created_at=dt.datetime(2026, 9, 9, tzinfo=UTC),
    )
    now = dt.datetime(2026, 9, 11, tzinfo=UTC)

    assert is_low_quality(post("Tesla is doing fine I guess"), author, now) is None


def test_low_quality_keeps_established_accounts():
    author = Author(
        id="1", username="matthew", followers=800,
        created_at=dt.datetime(2019, 1, 1, tzinfo=UTC),
    )

    assert is_low_quality(post("Tesla FSD saved me a commute today"), author) is None


def test_low_quality_passes_genuine_launch_excitement():
    text = "The new Rivian R2 pricing is actually competitive, big deal for the segment"

    assert is_low_quality(post(text)) is None
