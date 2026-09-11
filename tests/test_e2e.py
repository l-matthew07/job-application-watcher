"""End-to-end: the real CLI, config loader, pipeline, scorer and stores.

Only the HTTP transport is faked — everything below it is the shipping code
path, including YAML parsing, query construction, filtering, VADER scoring,
JSONL output and state persistence across runs.
"""

import io
import json

import pytest
import yaml

from tests.conftest import FakeXApi, make_page, make_post, make_user
from twitter_sentiment import cli
from twitter_sentiment.cli import EXIT_API, EXIT_OK
from twitter_sentiment.store import ResultStore, RunState

REAL_WORLD_POSTS = [
    # Matches "tesla" on a word boundary AND an exclude term, so it reaches
    # and exercises the exclusion rule. (A bare "Teslamania" would never get
    # that far — it fails the word-boundary term check first, as 1006 does.)
    make_post(1011, "Tesla fans packed the Teslamania convention hall today", likes=15),
    make_post(1010, "Tesla's new FSD build is genuinely excellent, best update yet", likes=420),
    make_post(1009, "third Tesla service center outage this week, absolutely terrible", likes=130),
    make_post(1008, "Tesla reported Q3 deliveries this morning", likes=12),
    make_post(1007, "TESLA GIVEAWAY!! RT to win a free Model 3 #tesla #ev #free", likes=3),
    make_post(1006, "Teslamania fan convention tickets are live", likes=8),
    make_post(1005, "Tesla est vraiment une catastrophe totale", lang="fr", likes=40),
    make_post(1004, "lol Tesla", likes=1),
    make_post(1003, "the Tesla app is so buggy since the redesign", likes=95),
    make_post(1002, "Rivian R2 pricing is actually competitive, love to see it", likes=210),
    make_post(1001, "my Rivian had a data breach notification, not great", likes=60),
]

USERS = [
    make_user(user_id="100", username="matthew", followers=5000),
    make_user(user_id="900", username="throwaway", followers=1,
              created_at="2026-09-10T00:00:00.000Z"),
]


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("X_BEARER_TOKEN", "test-token")
    monkeypatch.setattr(cli, "load_dotenv", lambda *a, **k: None)
    config = tmp_path / "companies.yaml"
    config.write_text(yaml.safe_dump({
        "output_path": str(tmp_path / "sentiment.jsonl"),
        "state_path": str(tmp_path / "state.json"),
        "defaults": {"lang": "en", "max_pages": 2},
        "extra_phrases": {"service center outage": -3.0},
        "companies": [
            {"name": "Tesla", "terms": ["tesla", "$TSLA"],
             "exclude_terms": ["teslamania"]},
            {"name": "Rivian", "terms": ["rivian"]},
        ],
    }), encoding="utf-8")
    return {
        "config": config,
        "out": tmp_path / "sentiment.jsonl",
        "state": tmp_path / "state.json",
    }


def invoke(workspace, api, extra=()):
    stream = io.StringIO()
    code = cli.main(
        ["--config", str(workspace["config"]), *extra],
        stream=stream,
        transport=api,
    )
    return code, stream.getvalue()


def two_company_api(posts=REAL_WORLD_POSTS):
    """One page per company, in config order."""
    tesla = [p for p in posts if "esla" in p["text"]]
    rivian = [p for p in posts if "ivian" in p["text"]]
    return FakeXApi([
        make_page(tesla, users=USERS),
        make_page(rivian, users=USERS),
    ])


def test_full_run_scores_filters_and_persists(workspace):
    api = two_company_api()

    code, out = invoke(workspace, api)

    assert code == EXIT_OK

    # Both companies were queried, with the config's own query strings.
    assert api.queries[0]["query"] == '(tesla OR $TSLA) -teslamania -is:retweet lang:en'
    assert api.queries[1]["query"] == "rivian -is:retweet lang:en"

    records = ResultStore(workspace["out"]).read_all()
    by_id = {r["id"]: r for r in records}

    # Kept and correctly labelled.
    assert by_id["1010"]["label"] == "positive"
    assert by_id["1009"]["label"] == "negative"
    assert by_id["1008"]["label"] == "neutral"
    assert by_id["1003"]["label"] == "negative"   # "buggy" — domain lexicon
    assert by_id["1002"]["label"] == "positive"
    assert by_id["1001"]["label"] == "negative"   # "data breach" — domain phrase

    # Filtered out, each for its own reason.
    for dropped in ("1011", "1007", "1006", "1005", "1004"):
        assert dropped not in by_id

    assert {r["company"] for r in records} == {"Tesla", "Rivian"}
    assert by_id["1010"]["url"] == "https://x.com/matthew/status/1010"
    assert by_id["1010"]["matched_terms"] == ["tesla"]


def test_human_output_reports_volume_drops_and_examples(workspace):
    _, out = invoke(workspace, two_company_api())

    assert "Tesla: 4 posts" in out
    assert "promo_spam=1" in out and "excluded_term=1" in out
    assert "no_term_match=1" in out
    assert "unsupported_lang=1" in out and "too_short=1" in out
    assert "most positive" in out and "most negative" in out
    assert "2 API request(s)" in out


def test_json_output_is_machine_readable(workspace):
    _, out = invoke(workspace, two_company_api(), ["--json"])
    payload = json.loads(out)

    assert payload["requests_made"] == 2
    tesla = next(c for c in payload["companies"] if c["company"] == "Tesla")
    assert tesla["total"] == 4
    assert tesla["kept"] == 4 and tesla["fetched"] == 9
    assert tesla["drops"]["promo_spam"] == 1
    assert -1.0 <= tesla["net_sentiment"] <= 1.0
    assert tesla["top_negative"][0]["label"] == "negative"


def test_config_extra_phrases_reach_the_scorer(workspace):
    """'service center outage' is only negative because the YAML says so."""
    _, out = invoke(workspace, two_company_api(), ["--json"])
    record = next(
        c for c in json.loads(out)["companies"] if c["company"] == "Tesla"
    )
    worst = record["top_negative"][0]

    assert "outage" in worst["text"]
    assert worst["sentiment"]["compound"] < -0.5


def test_second_run_resumes_from_the_high_water_mark(workspace):
    invoke(workspace, two_company_api())

    state = RunState(workspace["state"])
    assert state.since_id("Tesla") == "1011"
    assert state.since_id("Rivian") == "1002"

    # Nothing new since: the API returns empty, nothing is appended.
    before = len(ResultStore(workspace["out"]).read_all())
    api = FakeXApi([])
    code, out = invoke(workspace, api)

    assert code == EXIT_OK
    assert api.queries[0]["since_id"] == "1011"
    assert api.queries[1]["since_id"] == "1002"
    assert len(ResultStore(workspace["out"]).read_all()) == before
    assert "Tesla: no posts matched" in out


def test_second_run_appends_only_genuinely_new_posts(workspace):
    invoke(workspace, two_company_api())
    before = ResultStore(workspace["out"]).read_all()

    fresh = make_post(2000, "Tesla's charging network is seriously impressive", likes=50)
    code, _ = invoke(workspace, FakeXApi([
        make_page([fresh], users=USERS),
        make_page([], users=USERS),
    ]))
    after = ResultStore(workspace["out"]).read_all()

    assert code == EXIT_OK
    assert len(after) == len(before) + 1
    assert after[-1]["id"] == "2000" and after[-1]["label"] == "positive"
    assert RunState(workspace["state"]).since_id("Tesla") == "2000"


def test_reposted_copypasta_is_not_double_logged(workspace):
    invoke(workspace, two_company_api())
    before = len(ResultStore(workspace["out"]).read_all())

    # Same words, new ID, different trailing link — since_id can't catch this.
    repost = make_post(
        2001,
        "@someone Tesla's new FSD build is genuinely excellent, best update yet https://t.co/x",
    )
    _, out = invoke(workspace, FakeXApi([
        make_page([repost], users=USERS),
        make_page([], users=USERS),
    ]))

    assert len(ResultStore(workspace["out"]).read_all()) == before
    assert "duplicate=1" in out


def test_fresh_flag_rescans_the_window(workspace):
    invoke(workspace, two_company_api())

    api = two_company_api()
    invoke(workspace, api, ["--fresh"])

    assert "since_id" not in api.queries[0]


def test_only_flag_scopes_a_real_run(workspace):
    api = FakeXApi([make_page(
        [p for p in REAL_WORLD_POSTS if "ivian" in p["text"]], users=USERS
    )])

    _, out = invoke(workspace, api, ["--only", "Rivian", "--json"])
    payload = json.loads(out)

    assert len(api.queries) == 1
    assert [c["company"] for c in payload["companies"]] == ["Rivian"]


def test_pagination_across_a_real_run(workspace):
    tesla = [p for p in REAL_WORLD_POSTS if "esla" in p["text"]]
    api = FakeXApi([
        make_page(tesla[:4], users=USERS, next_token="tok"),
        make_page(tesla[4:], users=USERS),
        make_page([], users=USERS),
    ])

    _, out = invoke(workspace, api, ["--json"])
    tesla_run = next(c for c in json.loads(out)["companies"] if c["company"] == "Tesla")

    assert api.queries[1]["next_token"] == "tok"
    assert tesla_run["fetched"] == 9


def test_api_failure_midway_still_saves_state(workspace, monkeypatch, capsys):
    """Tesla succeeds, Rivian 503s: the Tesla mark must survive the failure."""
    from twitter_sentiment.client import Response

    class FlakyApi(FakeXApi):
        def get(self, url, params, headers, timeout):
            self.queries.append(dict(params))
            if "rivian" in params["query"]:
                return Response(503, payload={"title": "Service Unavailable"})
            return Response(200, payload=make_page(
                [p for p in REAL_WORLD_POSTS if "esla" in p["text"]], users=USERS
            ))

    code, _ = invoke(workspace, FlakyApi())

    assert code == EXIT_API
    assert "X API error" in capsys.readouterr().err
    assert RunState(workspace["state"]).since_id("Tesla") == "1011"
    assert ResultStore(workspace["out"]).read_all()


def test_dry_run_makes_no_requests(workspace):
    api = two_company_api()

    code, out = invoke(workspace, api, ["--dry-run"])

    assert code == EXIT_OK
    assert api.queries == []
    assert not workspace["out"].exists()
    assert "Tesla: (tesla OR $TSLA) -teslamania -is:retweet lang:en" in out
