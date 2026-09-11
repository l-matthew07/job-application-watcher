import pytest
import yaml

from twitter_sentiment.config import (
    CompanyConfig,
    ConfigError,
    bearer_token,
    config_from_terms,
    load_config,
    quote_term,
)


def write_config(tmp_path, payload):
    path = tmp_path / "companies.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return path


def test_single_term_query_has_no_redundant_parens():
    company = CompanyConfig(name="Rivian", terms=("rivian",))

    assert company.build_query() == "rivian -is:retweet lang:en"


def test_multi_term_query_is_an_or_group():
    company = CompanyConfig(name="Tesla", terms=("tesla", "$TSLA"))

    assert company.build_query() == "(tesla OR $TSLA) -is:retweet lang:en"


def test_multiword_terms_are_quoted_but_operators_are_not():
    company = CompanyConfig(
        name="Boston Dynamics", terms=("boston dynamics", "#BostonDynamics")
    )

    assert '"boston dynamics"' in company.build_query()
    assert '"#BostonDynamics"' not in company.build_query()


def test_exclude_terms_are_negated():
    company = CompanyConfig(
        name="Tesla", terms=("tesla",), exclude_terms=("nikola tesla", "teslacoil")
    )
    query = company.build_query()

    assert '-"nikola tesla"' in query
    assert "-teslacoil" in query


def test_reply_and_retweet_and_lang_toggles():
    company = CompanyConfig(
        name="X", terms=("x",), exclude_retweets=False,
        exclude_replies=True, lang="",
    )

    assert company.build_query() == "x -is:reply"


def test_extra_query_is_appended():
    company = CompanyConfig(name="Tesla", terms=("tesla",), extra_query="has:links")

    assert company.build_query().endswith("has:links")


def test_over_length_query_is_rejected_before_the_api_sees_it():
    company = CompanyConfig(name="Wide", terms=tuple(f"term{i}" for i in range(200)))

    with pytest.raises(ConfigError, match="over the 512-char limit"):
        company.build_query()


def test_query_length_limit_is_configurable():
    company = CompanyConfig(name="Tesla", terms=("tesla",))

    assert company.build_query(max_length=1024)
    with pytest.raises(ConfigError):
        company.build_query(max_length=5)


def test_already_quoted_terms_are_not_double_quoted():
    assert quote_term('"boston dynamics"') == '"boston dynamics"'


def test_empty_term_is_rejected():
    with pytest.raises(ConfigError):
        quote_term("   ")


@pytest.mark.parametrize(
    "kwargs,message",
    [
        ({"name": "", "terms": ("a",)}, "missing a name"),
        ({"name": "A", "terms": ()}, "at least one search term"),
        ({"name": "A", "terms": ("a",), "max_results": 9}, "max_results"),
        ({"name": "A", "terms": ("a",), "max_results": 101}, "max_results"),
        ({"name": "A", "terms": ("a",), "max_pages": 0}, "max_pages"),
    ],
)
def test_company_validation(kwargs, message):
    with pytest.raises(ConfigError, match=message):
        CompanyConfig(**kwargs)


def test_load_config_applies_defaults_and_overrides(tmp_path):
    path = write_config(tmp_path, {
        "defaults": {"lang": "en", "max_pages": 2},
        "output_path": "out.jsonl",
        "companies": [
            {"name": "Tesla", "terms": ["tesla", "$TSLA"]},
            {"name": "Rivian", "terms": ["rivian"], "max_pages": 5},
        ],
    })
    config = load_config(path)

    assert config.output_path == "out.jsonl"
    assert config.company("tesla").max_pages == 2
    assert config.company("Rivian").max_pages == 5


def test_company_without_terms_watches_its_own_name(tmp_path):
    path = write_config(tmp_path, {"companies": [{"name": "Rivian"}]})

    assert load_config(path).company("Rivian").terms == ("Rivian",)


def test_string_terms_are_accepted_as_a_single_term(tmp_path):
    path = write_config(tmp_path, {
        "companies": [{"name": "Tesla", "terms": "tesla", "exclude_terms": "teslacoil"}]
    })
    company = load_config(path).company("Tesla")

    assert company.terms == ("tesla",)
    assert company.exclude_terms == ("teslacoil",)


def test_extra_lexicon_and_phrases_round_trip(tmp_path):
    path = write_config(tmp_path, {
        "extra_lexicon": {"vaporware": -2.5},
        "extra_phrases": {"chip shortage": -2.0},
        "companies": [{"name": "Tesla"}],
    })
    config = load_config(path)

    assert config.extra_lexicon == {"vaporware": -2.5}
    assert config.extra_phrases == {"chip shortage": -2.0}


def test_duplicate_companies_are_rejected(tmp_path):
    path = write_config(tmp_path, {
        "companies": [{"name": "Tesla"}, {"name": "tesla"}]
    })

    with pytest.raises(ConfigError, match="duplicate company"):
        load_config(path)


def test_typo_in_a_company_key_is_rejected(tmp_path):
    path = write_config(tmp_path, {
        "companies": [{"name": "Tesla", "exclude_retweet": True}]
    })

    with pytest.raises(ConfigError, match="unknown keys"):
        load_config(path)


def test_over_length_query_fails_at_load_time(tmp_path):
    path = write_config(tmp_path, {
        "companies": [{"name": "Wide", "terms": [f"term{i}" for i in range(200)]}]
    })

    with pytest.raises(ConfigError, match="over the 512-char limit"):
        load_config(path)


def test_missing_file_is_reported(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml")


def test_empty_company_list_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="no companies"):
        load_config(write_config(tmp_path, {"companies": []}))


def test_non_mapping_top_level_is_rejected(tmp_path):
    path = tmp_path / "companies.yaml"
    path.write_text("- just\n- a list\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="must be a mapping"):
        load_config(path)


def test_non_mapping_company_entry_is_rejected(tmp_path):
    path = write_config(tmp_path, {"companies": ["Tesla"]})

    with pytest.raises(ConfigError, match="must be a mapping"):
        load_config(path)


def test_company_lookup_is_case_insensitive_and_errors_clearly(tmp_path):
    config = load_config(write_config(tmp_path, {"companies": [{"name": "Tesla"}]}))

    assert config.company("TESLA").name == "Tesla"
    with pytest.raises(ConfigError, match="no company named"):
        config.company("Rivian")


def test_config_from_terms_builds_a_one_company_run():
    config = config_from_terms("Tesla", ["tesla", "$TSLA"], max_pages=1)

    assert len(config.companies) == 1
    assert config.companies[0].max_pages == 1
    assert config.companies[0].build_query().startswith("(tesla OR $TSLA)")


def test_config_from_terms_defaults_to_the_name():
    assert config_from_terms("Rivian", []).companies[0].terms == ("Rivian",)


def test_bearer_token_accepts_either_env_name():
    assert bearer_token({"X_BEARER_TOKEN": "abc"}) == "abc"
    assert bearer_token({"TWITTER_BEARER_TOKEN": "legacy"}) == "legacy"


def test_bearer_token_prefers_the_current_name():
    assert bearer_token({"X_BEARER_TOKEN": "new", "TWITTER_BEARER_TOKEN": "old"}) == "new"


def test_missing_bearer_token_is_actionable():
    with pytest.raises(ConfigError, match="X_BEARER_TOKEN is not set"):
        bearer_token({})
