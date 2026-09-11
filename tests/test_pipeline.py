import datetime as dt

from tests.conftest import FakeXApi, make_page, make_post, make_user
from twitter_sentiment.config import CompanyConfig
from twitter_sentiment.pipeline import run_company
from twitter_sentiment.store import RunState

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 9, 11, tzinfo=UTC)

TESLA = CompanyConfig(name="Tesla", terms=("tesla", "$TSLA"), max_pages=2)


def run(pages, client_factory, scorer, company=TESLA, state=None):
    api = FakeXApi(pages)
    result = run_company(client_factory(api), scorer, company, state, now=NOW)
    return result, api


def test_relevant_posts_are_scored_and_labelled(client_factory, scorer):
    result, _ = run([make_page(
        [
            make_post(3, "Tesla's new FSD build is genuinely excellent"),
            make_post(2, "my Tesla bricked itself after the update, awful"),
            make_post(1, "Tesla reported deliveries this morning"),
        ],
        users=[make_user()],
    )], client_factory, scorer)

    assert result.kept == 3
    labels = {p.post.id: p.sentiment.label for p in result.scored}
    assert labels == {"3": "positive", "2": "negative", "1": "neutral"}


def test_summary_is_attached_to_the_run(client_factory, scorer):
    result, _ = run([make_page(
        [make_post(2, "Tesla support was wonderful today"),
         make_post(1, "Tesla service was a horrible experience")],
        users=[make_user()],
    )], client_factory, scorer)

    assert result.summary.total == 2
    assert result.summary.positive == 1 and result.summary.negative == 1


def test_author_is_attached_from_the_expansion(client_factory, scorer):
    result, _ = run([make_page(
        [make_post(1, "Tesla is doing great work here", author_id="777")],
        users=[make_user(user_id="777", username="matthew")],
    )], client_factory, scorer)

    assert result.scored[0].author.username == "matthew"
    assert result.scored[0].to_record()["url"].endswith("/matthew/status/1")


def test_posts_without_the_term_are_dropped(client_factory, scorer):
    # The API tokenizes loosely; a word-boundary re-check is what catches this.
    result, _ = run([make_page(
        [make_post(2, "Teslamania fan convention was great"),
         make_post(1, "Tesla build quality is great")],
        users=[make_user()],
    )], client_factory, scorer)

    assert result.kept == 1
    assert result.drops == {"no_term_match": 1}


def test_excluded_terms_are_dropped(client_factory, scorer):
    company = CompanyConfig(
        name="Tesla", terms=("tesla",), exclude_terms=("nikola tesla",)
    )
    result, _ = run([make_page(
        [make_post(1, "Nikola Tesla was a brilliant inventor")],
        users=[make_user()],
    )], client_factory, scorer, company=company)

    assert result.kept == 0
    assert result.drops == {"excluded_term": 1}


def test_spam_is_dropped_with_its_reason(client_factory, scorer):
    result, _ = run([make_page(
        [make_post(1, "TESLA GIVEAWAY!! RT to win a free Model 3 today")],
        users=[make_user()],
    )], client_factory, scorer)

    assert result.drops == {"promo_spam": 1}


def test_throwaway_accounts_are_dropped(client_factory, scorer):
    result, _ = run([make_page(
        [make_post(1, "Tesla is fine I suppose", author_id="9")],
        users=[make_user(user_id="9", username="brandnew", followers=1,
                         created_at="2026-09-10T00:00:00.000Z")],
    )], client_factory, scorer)

    assert result.drops == {"throwaway_account": 1}


def test_unsupported_language_is_dropped_not_scored_as_neutral(client_factory, scorer):
    result, _ = run([make_page(
        [make_post(1, "Tesla est vraiment une catastrophe totale", lang="fr")],
        users=[make_user()],
    )], client_factory, scorer)

    assert result.kept == 0
    assert result.drops == {"unsupported_lang": 1}
    assert result.summary.total == 0


def test_copypasta_is_deduped_within_a_run(client_factory, scorer):
    text = "Tesla's charging network is the real moat here"
    result, _ = run([make_page(
        [make_post(2, f"{text} https://t.co/aaa"),
         make_post(1, f"@someone {text} https://t.co/bbb")],
        users=[make_user()],
    )], client_factory, scorer)

    assert result.kept == 1
    assert result.drops == {"duplicate": 1}


def test_copypasta_is_deduped_across_runs(tmp_path, client_factory, scorer):
    state = RunState(tmp_path / "state.json")
    text = "Tesla's charging network is the real moat here"
    page = lambda pid: make_page([make_post(pid, text)], users=[make_user()])

    first, _ = run([page(1)], client_factory, scorer, state=state)
    second, _ = run([page(2)], client_factory, scorer, state=state)

    assert first.kept == 1
    assert second.kept == 0 and second.drops == {"duplicate": 1}


def test_pagination_is_followed(client_factory, scorer):
    result, api = run([
        make_page([make_post(2, "Tesla is great stuff")],
                  users=[make_user()], next_token="tok"),
        make_page([make_post(1, "Tesla is terrible stuff")], users=[make_user()]),
    ], client_factory, scorer)

    assert result.fetched == 2 and result.kept == 2
    assert api.queries[1]["next_token"] == "tok"


def test_query_is_built_from_the_company_config(client_factory, scorer):
    _, api = run([], client_factory, scorer)

    assert api.queries[0]["query"] == "(tesla OR $TSLA) -is:retweet lang:en"
    assert api.queries[0]["max_results"] == 100


def test_since_id_is_sent_and_advanced(tmp_path, client_factory, scorer):
    state = RunState(tmp_path / "state.json")
    state.advance("Tesla", "500")

    result, api = run([make_page(
        [make_post(900, "Tesla is great stuff")], users=[make_user()],
    )], client_factory, scorer, state=state)

    assert api.queries[0]["since_id"] == "500"
    assert result.newest_id == "900"
    assert state.since_id("Tesla") == "900"


def test_high_water_mark_advances_past_filtered_posts(tmp_path, client_factory, scorer):
    """A window of pure spam must still move since_id, or it re-fetches forever."""
    state = RunState(tmp_path / "state.json")
    result, _ = run([make_page(
        [make_post(900, "TESLA GIVEAWAY! RT to win big")], users=[make_user()],
    )], client_factory, scorer, state=state)

    assert result.kept == 0
    assert state.since_id("Tesla") == "900"


def test_no_state_still_runs(client_factory, scorer):
    result, api = run([make_page(
        [make_post(1, "Tesla is great stuff")], users=[make_user()],
    )], client_factory, scorer, state=None)

    assert "since_id" not in api.queries[0]
    assert result.kept == 1


def test_empty_result_is_not_an_error(client_factory, scorer):
    result, _ = run([], client_factory, scorer)

    assert result.fetched == 0 and result.kept == 0
    assert result.summary.summary() == "Tesla: no posts matched"


def test_missing_author_expansion_does_not_break_scoring(client_factory, scorer):
    result, _ = run([make_page([make_post(1, "Tesla is great stuff")])],
                    client_factory, scorer)

    assert result.kept == 1 and result.scored[0].author is None


def test_drop_report_is_ordered_by_count(client_factory, scorer):
    result, _ = run([make_page(
        [make_post(4, "Teslamania convention one"),
         make_post(3, "Teslamania convention two"),
         make_post(2, "TESLA GIVEAWAY! RT to win now")],
        users=[make_user()],
    )], client_factory, scorer)

    assert result.drop_report() == "no_term_match=2, promo_spam=1"


def test_drop_report_when_nothing_dropped(client_factory, scorer):
    result, _ = run([make_page([make_post(1, "Tesla is great stuff")],
                               users=[make_user()])], client_factory, scorer)

    assert result.drop_report() == "none dropped"
