"""Lambda handler tests: config resolution, metrics, and the failure paths
that only matter because it's Lambda (timeouts, partial runs, state saving)."""

import json

import boto3
import pytest
import yaml
from moto import mock_aws

from tests.conftest import FakeXApi, make_page, make_post, make_user
from twitter_sentiment import handler
from twitter_sentiment.aws import DynamoRunState, S3ResultStore
from twitter_sentiment.client import XApiError, XRateLimitError
from twitter_sentiment.config import ConfigError
from twitter_sentiment.models import CompanySentiment
from twitter_sentiment.pipeline import CompanyRun

REGION = "us-east-1"
BUCKET = "sentiment-results"
TABLE = "sentiment-state"

CONFIG_YAML = yaml.safe_dump({
    "defaults": {"lang": "en", "max_pages": 1},
    "companies": [
        {"name": "Tesla", "terms": ["tesla"]},
        {"name": "Rivian", "terms": ["rivian"]},
    ],
})


class FakeContext:
    def __init__(self, remaining_ms=300_000):
        self._remaining = remaining_ms

    def get_remaining_time_in_millis(self):
        return self._remaining


@pytest.fixture
def aws_env(monkeypatch, tmp_path):
    for key, value in {
        "AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing", "AWS_DEFAULT_REGION": REGION,
    }.items():
        monkeypatch.setenv(key, value)
    with mock_aws():
        boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)
        boto3.client("dynamodb", region_name=REGION).create_table(
            TableName=TABLE, BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
        )
        config_path = tmp_path / "companies.yaml"
        config_path.write_text(CONFIG_YAML, encoding="utf-8")
        monkeypatch.setenv("RESULTS_BUCKET", BUCKET)
        monkeypatch.setenv("STATE_TABLE", TABLE)
        monkeypatch.setenv("CONFIG_PATH", str(config_path))
        monkeypatch.setenv("X_BEARER_TOKEN", "test-token")
        # Reset the cross-invocation scorer cache between tests.
        handler._scorer, handler._scorer_key = None, None
        yield {"config_path": config_path}


# Captured once at import: re-reading handler.XSearchClient inside the helper
# would pick up a previous test's patch and re-wrap it.
REAL_CLIENT = handler.XSearchClient


def scripted_client(monkeypatch, pages, transport=None, **client_kwargs):
    """Point the handler at a fake X API, keeping every other layer real."""
    api = transport if transport is not None else FakeXApi(pages)
    monkeypatch.setattr(
        handler, "XSearchClient",
        lambda token, **kw: REAL_CLIENT(
            token, transport=api, sleep=lambda _: None, **client_kwargs, **kw
        ),
    )
    return api


TESLA_PAGE = lambda: make_page(
    [make_post(1010, "Tesla's new FSD build is genuinely excellent", likes=40),
     make_post(1009, "the Tesla app is so buggy since the redesign", likes=9)],
    users=[make_user()],
)
RIVIAN_PAGE = lambda: make_page(
    [make_post(1002, "Rivian R2 pricing is actually competitive, love it")],
    users=[make_user()],
)


# --------------------------------------------------------------------------
# Config resolution
# --------------------------------------------------------------------------

def test_config_loads_from_the_bundled_file(aws_env):
    config = handler.load_run_config()

    assert [c.name for c in config.companies] == ["Tesla", "Rivian"]


def test_config_loads_from_s3_when_set(aws_env, monkeypatch):
    """S3 config is what lets the company list change without a redeploy."""
    boto3.client("s3", region_name=REGION).put_object(
        Bucket=BUCKET, Key="config/companies.yaml",
        Body=yaml.safe_dump({"companies": [{"name": "Anduril", "terms": ["anduril"]}]}),
    )
    monkeypatch.setenv("CONFIG_S3_URI", f"s3://{BUCKET}/config/companies.yaml")

    assert [c.name for c in handler.load_run_config().companies] == ["Anduril"]


def test_s3_config_takes_precedence_over_the_bundled_file(aws_env, monkeypatch):
    boto3.client("s3", region_name=REGION).put_object(
        Bucket=BUCKET, Key="c.yaml",
        Body=yaml.safe_dump({"companies": [{"name": "Anduril"}]}),
    )
    monkeypatch.setenv("CONFIG_S3_URI", f"s3://{BUCKET}/c.yaml")

    assert [c.name for c in handler.load_run_config().companies] == ["Anduril"]


@pytest.mark.parametrize("uri", ["https://example.com/c.yaml", "s3://bucket-only"])
def test_malformed_config_uri_is_rejected(aws_env, monkeypatch, uri):
    monkeypatch.setenv("CONFIG_S3_URI", uri)

    with pytest.raises(ConfigError):
        handler.load_run_config()


def test_invalid_s3_config_fails_validation(aws_env, monkeypatch):
    boto3.client("s3", region_name=REGION).put_object(
        Bucket=BUCKET, Key="bad.yaml", Body=yaml.safe_dump({"companies": []}))
    monkeypatch.setenv("CONFIG_S3_URI", f"s3://{BUCKET}/bad.yaml")

    with pytest.raises(ConfigError, match="no companies"):
        handler.load_run_config()


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------

def test_emf_payload_has_the_shape_cloudwatch_parses():
    run = CompanyRun(
        company="Tesla",
        summary=CompanySentiment(company="Tesla", total=4, positive=2, neutral=1,
                                 negative=1, mean_compound=0.2,
                                 weighted_compound=0.25, total_engagement=90),
        drops={"promo_spam": 2},
        fetched=9,
    )
    lines = []
    payload = emit = handler.emit_metrics(run, clock=lambda: 1_700_000_000.0,
                                          out=lines.append)

    assert json.loads(lines[0]) == payload
    meta = payload["_aws"]["CloudWatchMetrics"][0]
    assert payload["_aws"]["Timestamp"] == 1_700_000_000_000
    assert meta["Namespace"] == "TwitterSentiment"
    assert meta["Dimensions"] == [["Company"]]
    # Every declared metric must have a value at the root, or CloudWatch
    # silently drops it.
    for metric in meta["Metrics"]:
        assert metric["Name"] in payload


def test_emf_values_come_from_the_run():
    run = CompanyRun(
        company="Tesla",
        summary=CompanySentiment(company="Tesla", total=4, positive=3, neutral=0,
                                 negative=1, mean_compound=0.5,
                                 weighted_compound=0.6, total_engagement=90),
        drops={"promo_spam": 2, "duplicate": 3},
        fetched=9,
    )
    payload = handler.emit_metrics(run, out=lambda _: None)

    assert payload["PostsFetched"] == 9
    assert payload["PostsDropped"] == 5
    assert payload["NetSentiment"] == 0.5
    assert payload["drops"] == {"promo_spam": 2, "duplicate": 3}


def test_metrics_namespace_is_configurable():
    run = CompanyRun(company="Tesla", summary=CompanySentiment(company="Tesla"))
    payload = handler.emit_metrics(run, namespace="Custom", out=lambda _: None)

    assert payload["_aws"]["CloudWatchMetrics"][0]["Namespace"] == "Custom"


# --------------------------------------------------------------------------
# Invocation
# --------------------------------------------------------------------------

def test_run_writes_results_to_s3_and_state_to_dynamo(aws_env, monkeypatch):
    scripted_client(monkeypatch, [TESLA_PAGE(), RIVIAN_PAGE()])

    result = handler.lambda_handler({}, FakeContext())

    assert [c["company"] for c in result["companies"]] == ["Tesla", "Rivian"]
    assert result["api_requests"] == 2

    records = S3ResultStore(BUCKET).read_all()
    assert {r["id"] for r in records} == {"1010", "1009", "1002"}

    state = DynamoRunState(TABLE)
    assert state.since_id("Tesla") == "1010"
    assert state.since_id("Rivian") == "1002"


def test_second_run_resumes_from_dynamo(aws_env, monkeypatch):
    scripted_client(monkeypatch, [TESLA_PAGE(), RIVIAN_PAGE()])
    handler.lambda_handler({}, FakeContext())

    api = scripted_client(monkeypatch, [])
    handler.lambda_handler({}, FakeContext())

    assert api.queries[0]["since_id"] == "1010"
    assert api.queries[1]["since_id"] == "1002"


def test_event_can_scope_the_run_to_named_companies(aws_env, monkeypatch):
    api = scripted_client(monkeypatch, [RIVIAN_PAGE()])

    result = handler.lambda_handler({"companies": ["rivian"]}, FakeContext())

    assert len(api.queries) == 1
    assert [c["company"] for c in result["companies"]] == ["Rivian"]


def test_out_of_time_stops_cleanly_and_still_saves(aws_env, monkeypatch):
    """Being killed mid-run skips the save entirely — stop early instead."""
    scripted_client(monkeypatch, [TESLA_PAGE(), RIVIAN_PAGE()])

    class DrainingContext:
        def __init__(self):
            self.calls = 0

        def get_remaining_time_in_millis(self):
            self.calls += 1
            return 300_000 if self.calls == 1 else 1_000

    result = handler.lambda_handler({}, DrainingContext())

    assert [c["company"] for c in result["companies"]] == ["Tesla"]
    assert result["skipped"] == ["Rivian"]
    assert DynamoRunState(TABLE).since_id("Tesla") == "1010"
    assert DynamoRunState(TABLE).since_id("Rivian") is None


def test_api_failure_raises_but_banks_completed_companies(aws_env, monkeypatch):
    """A crash on company two must not discard company one's high-water mark."""
    from twitter_sentiment.client import Response

    class HalfBrokenApi(FakeXApi):
        def get(self, url, params, headers, timeout):
            self.queries.append(dict(params))
            if "rivian" in params["query"]:
                return Response(503, payload={"title": "Service Unavailable"})
            return Response(200, payload=TESLA_PAGE())

    scripted_client(monkeypatch, [], transport=HalfBrokenApi(), max_retries=1)

    with pytest.raises(XApiError):
        handler.lambda_handler({}, FakeContext())

    assert DynamoRunState(TABLE).since_id("Tesla") == "1010"
    assert S3ResultStore(BUCKET).read_all()


def test_rate_limit_error_propagates_for_the_dlq(aws_env, monkeypatch):
    from twitter_sentiment.client import Response

    class LimitedApi(FakeXApi):
        def get(self, url, params, headers, timeout):
            self.queries.append(dict(params))
            return Response(429)

    scripted_client(monkeypatch, [], transport=LimitedApi(), max_retries=0)

    with pytest.raises(XRateLimitError):
        handler.lambda_handler({}, FakeContext())


def test_missing_token_fails_the_invocation(aws_env, monkeypatch):
    monkeypatch.delenv("X_BEARER_TOKEN", raising=False)
    scripted_client(monkeypatch, [])

    with pytest.raises(RuntimeError, match="no X bearer token"):
        handler.lambda_handler({}, FakeContext())


def test_scorer_is_reused_across_invocations(aws_env, monkeypatch):
    """VADER's lexicon load is ~1s; paying it every scheduled run is waste."""
    scripted_client(monkeypatch, [TESLA_PAGE(), RIVIAN_PAGE()])
    handler.lambda_handler({}, FakeContext())
    first = handler._scorer

    scripted_client(monkeypatch, [])
    handler.lambda_handler({}, FakeContext())

    assert handler._scorer is first


def test_scorer_is_rebuilt_when_the_lexicon_config_changes(aws_env, monkeypatch):
    scripted_client(monkeypatch, [TESLA_PAGE(), RIVIAN_PAGE()])
    handler.lambda_handler({}, FakeContext())
    first = handler._scorer

    aws_env["config_path"].write_text(yaml.safe_dump({
        "extra_lexicon": {"vaporware": -2.5},
        "companies": [{"name": "Tesla", "terms": ["tesla"]}],
    }), encoding="utf-8")
    scripted_client(monkeypatch, [])
    handler.lambda_handler({}, FakeContext())

    assert handler._scorer is not first
    assert handler._scorer.score("pure vaporware").label == "negative"


def test_missing_required_env_is_a_clear_failure(aws_env, monkeypatch):
    monkeypatch.delenv("RESULTS_BUCKET")
    scripted_client(monkeypatch, [])

    with pytest.raises(KeyError, match="RESULTS_BUCKET"):
        handler.lambda_handler({}, FakeContext())


def test_handler_works_without_a_context_object(aws_env, monkeypatch):
    """Manual `aws lambda invoke` and local smoke tests pass no context."""
    scripted_client(monkeypatch, [TESLA_PAGE(), RIVIAN_PAGE()])

    assert len(handler.lambda_handler({}, None)["companies"]) == 2
