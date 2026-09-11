"""End-to-end on the deployed shape: repeated scheduled runs against emulated AWS.

Only the HTTP call to X is faked. Config loading, filtering, VADER scoring, S3
partitioning, DynamoDB state, metric emission and the Lambda handler's own
control flow are all the real code, and the deployment package is the one
deploy.sh actually stages.
"""

import json
import pathlib
import re
import shutil
import subprocess
import sys

import boto3
import pytest
import yaml
from moto import mock_aws

from tests.conftest import FakeXApi, make_page, make_post, make_user
from twitter_sentiment import handler
from twitter_sentiment.aws import DynamoRunState, S3ResultStore

REGION = "us-east-1"
BUCKET = "sentiment-e2e"
TABLE = "sentiment-e2e-state"
REPO = pathlib.Path(__file__).resolve().parents[1]

COMPANIES = yaml.safe_dump({
    "defaults": {"lang": "en", "max_pages": 2},
    "extra_phrases": {"service center outage": -3.0},
    "companies": [
        {"name": "Tesla", "terms": ["tesla", "$TSLA"], "exclude_terms": ["teslamania"]},
        {"name": "Rivian", "terms": ["rivian"]},
    ],
})

REAL_CLIENT = handler.XSearchClient


class FakeContext:
    def get_remaining_time_in_millis(self):
        return 300_000


@pytest.fixture
def deployed(monkeypatch, tmp_path):
    """A stack's worth of resources, wired the way the template wires them."""
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
        secret_arn = boto3.client("secretsmanager", region_name=REGION).create_secret(
            Name="x-bearer", SecretString=json.dumps({"X_BEARER_TOKEN": "deployed-token"})
        )["ARN"]

        config_path = tmp_path / "companies.yaml"
        config_path.write_text(COMPANIES, encoding="utf-8")

        # Exactly the env the template sets.
        for key, value in {
            "RESULTS_BUCKET": BUCKET,
            "STATE_TABLE": TABLE,
            "RESULTS_PREFIX": "raw",
            "X_BEARER_SECRET_ID": secret_arn,
            "METRICS_NAMESPACE": "TwitterSentiment",
            "CONFIG_PATH": str(config_path),
        }.items():
            monkeypatch.setenv(key, value)
        for key in ("X_BEARER_TOKEN", "TWITTER_BEARER_TOKEN", "CONFIG_S3_URI"):
            monkeypatch.delenv(key, raising=False)

        handler._scorer, handler._scorer_key = None, None
        yield {"config_path": config_path, "secret_arn": secret_arn}


def invoke(monkeypatch, pages, event=None, capsys=None):
    """One scheduled invocation against a scripted X API."""
    api = FakeXApi(pages)
    monkeypatch.setattr(
        handler, "XSearchClient",
        lambda token, **kw: REAL_CLIENT(token, transport=api, sleep=lambda _: None, **kw),
    )
    result = handler.lambda_handler(event or {}, FakeContext())
    return result, api


def tesla_page(*posts):
    return make_page(list(posts), users=[
        make_user(user_id="100", username="matthew", followers=5000),
        make_user(user_id="900", username="throwaway", followers=1,
                  created_at="2026-09-10T00:00:00.000Z"),
    ])


DAY_ONE_TESLA = lambda: tesla_page(
    make_post(1010, "Tesla's new FSD build is genuinely excellent, best update yet", likes=420),
    make_post(1009, "third Tesla service center outage this week, absolutely terrible", likes=130),
    make_post(1008, "Tesla reported Q3 deliveries this morning", likes=12),
    make_post(1007, "TESLA GIVEAWAY!! RT to win a free Model 3 #tesla #ev #free", likes=3),
    make_post(1006, "Tesla fans packed the Teslamania convention hall", likes=15),
    make_post(1005, "Tesla est vraiment une catastrophe totale", lang="fr", likes=40),
    make_post(1004, "lol Tesla", likes=1),
)
DAY_ONE_RIVIAN = lambda: tesla_page(
    make_post(1002, "Rivian R2 pricing is actually competitive, love to see it", likes=210),
    make_post(1001, "my Rivian had a data breach notification, not great", likes=60),
)


def test_scheduled_run_lands_everything_in_the_right_place(deployed, monkeypatch, capsys):
    result, api = invoke(monkeypatch, [DAY_ONE_TESLA(), DAY_ONE_RIVIAN()])

    # Queries came from the config, not from defaults.
    assert api.queries[0]["query"] == "(tesla OR $TSLA) -teslamania -is:retweet lang:en"
    assert api.queries[1]["query"] == "rivian -is:retweet lang:en"

    # Posts in S3, correctly partitioned and correctly scored.
    keys = [o["Key"] for o in boto3.client("s3", region_name=REGION)
            .list_objects_v2(Bucket=BUCKET)["Contents"]]
    assert any("raw/company=tesla/dt=" in k for k in keys)
    assert any("raw/company=rivian/dt=" in k for k in keys)

    by_id = {r["id"]: r for r in S3ResultStore(BUCKET).read_all()}
    assert by_id["1010"]["label"] == "positive"
    assert by_id["1009"]["label"] == "negative"     # config's extra_phrases
    assert by_id["1008"]["label"] == "neutral"
    assert by_id["1001"]["label"] == "negative"     # built-in domain phrase
    for dropped in ("1007", "1006", "1005", "1004"):
        assert dropped not in by_id

    # State in DynamoDB, under the repo's key convention.
    item = boto3.resource("dynamodb", region_name=REGION).Table(TABLE).get_item(
        Key={"pk": "SENTIMENT#tesla", "sk": "STATE"})["Item"]
    assert item["since_id"] == "1010"
    # The ring records kept posts only — a dropped post is not something a
    # later run should refuse to re-consider.
    tesla = next(c for c in result["companies"] if c["company"] == "Tesla")
    assert len(item["seen"]) == tesla["kept"] == 3

    assert result["api_requests"] == 2
    assert [c["company"] for c in result["companies"]] == ["Tesla", "Rivian"]
    assert tesla["drops"] == {
        "promo_spam": 1, "excluded_term": 1, "unsupported_lang": 1, "too_short": 1,
    }


def test_metrics_are_emitted_per_company(deployed, monkeypatch, capsys):
    invoke(monkeypatch, [DAY_ONE_TESLA(), DAY_ONE_RIVIAN()])

    emitted = [
        json.loads(line) for line in capsys.readouterr().out.splitlines()
        if line.startswith("{") and '"_aws"' in line
    ]

    assert [m["Company"] for m in emitted] == ["Tesla", "Rivian"]
    tesla = emitted[0]
    assert tesla["PostsFetched"] == 7 and tesla["PostsKept"] == 3
    assert tesla["drops"]["promo_spam"] == 1
    assert tesla["_aws"]["CloudWatchMetrics"][0]["Namespace"] == "TwitterSentiment"


def test_consecutive_runs_resume_and_never_re_emit(deployed, monkeypatch):
    invoke(monkeypatch, [DAY_ONE_TESLA(), DAY_ONE_RIVIAN()])
    after_first = len(S3ResultStore(BUCKET).read_all())

    # Run 2: nothing new. since_id is sent; nothing is written.
    _, api = invoke(monkeypatch, [])
    assert api.queries[0]["since_id"] == "1010"
    assert api.queries[1]["since_id"] == "1002"
    assert len(S3ResultStore(BUCKET).read_all()) == after_first

    # Run 3: one genuinely new post plus the same copypasta under a new ID.
    _, _ = invoke(monkeypatch, [
        tesla_page(
            make_post(2000, "Tesla's charging network is seriously impressive", likes=50),
            make_post(2001, "@x Tesla's new FSD build is genuinely excellent, "
                            "best update yet https://t.co/z"),
        ),
        make_page([], users=[]),
    ])

    records = S3ResultStore(BUCKET).read_all()
    assert len(records) == after_first + 1
    assert records[-1]["id"] == "2000"
    assert DynamoRunState(TABLE).since_id("Tesla") == "2001"


def test_each_run_writes_its_own_objects(deployed, monkeypatch):
    """Three runs must not overwrite each other's S3 objects."""
    invoke(monkeypatch, [DAY_ONE_TESLA(), DAY_ONE_RIVIAN()])
    invoke(monkeypatch, [tesla_page(make_post(2000, "Tesla shipped something good")),
                         make_page([], users=[])])
    invoke(monkeypatch, [tesla_page(make_post(3000, "Tesla did a terrible thing")),
                         make_page([], users=[])])

    tesla_keys = [
        o["Key"] for o in boto3.client("s3", region_name=REGION).list_objects_v2(
            Bucket=BUCKET, Prefix="raw/company=tesla/")["Contents"]
    ]
    assert len(tesla_keys) == 3
    assert len(set(tesla_keys)) == 3


def test_config_change_in_s3_takes_effect_without_redeploying(deployed, monkeypatch):
    boto3.client("s3", region_name=REGION).put_object(
        Bucket=BUCKET, Key="config/companies.yaml",
        Body=yaml.safe_dump({"companies": [{"name": "Anduril", "terms": ["anduril"]}]}),
    )
    monkeypatch.setenv("CONFIG_S3_URI", f"s3://{BUCKET}/config/companies.yaml")

    result, api = invoke(monkeypatch, [
        make_page([make_post(5000, "Anduril's new system looks genuinely impressive")],
                  users=[make_user()])
    ])

    assert api.queries[0]["query"] == "anduril -is:retweet lang:en"
    assert [c["company"] for c in result["companies"]] == ["Anduril"]
    assert S3ResultStore(BUCKET).read_all(company="Anduril")[0]["id"] == "5000"


def test_token_is_read_from_secrets_manager(deployed, monkeypatch):
    """The deployed path never sees X_BEARER_TOKEN — only the secret ARN."""
    captured = {}

    def capture(token, **kw):
        captured["token"] = token
        return REAL_CLIENT(token, transport=FakeXApi([]), sleep=lambda _: None, **kw)

    monkeypatch.setattr(handler, "XSearchClient", capture)
    handler.lambda_handler({}, FakeContext())

    assert captured["token"] == "deployed-token"


def test_failure_partway_banks_completed_work(deployed, monkeypatch):
    from twitter_sentiment.client import Response, XApiError

    class HalfBroken(FakeXApi):
        def get(self, url, params, headers, timeout):
            self.queries.append(dict(params))
            if "rivian" in params["query"]:
                return Response(503, payload={"title": "Service Unavailable"})
            return Response(200, payload=DAY_ONE_TESLA())

    monkeypatch.setattr(
        handler, "XSearchClient",
        lambda token, **kw: REAL_CLIENT(token, transport=HalfBroken(),
                                        sleep=lambda _: None, max_retries=1, **kw),
    )

    with pytest.raises(XApiError):
        handler.lambda_handler({}, FakeContext())

    # The invocation failed (so the DLQ sees it) but Tesla's work survived.
    assert DynamoRunState(TABLE).since_id("Tesla") == "1010"
    assert DynamoRunState(TABLE).since_id("Rivian") is None
    assert S3ResultStore(BUCKET).read_all(company="Tesla")


# --------------------------------------------------------------------------
# The deployment package itself
# --------------------------------------------------------------------------

def test_deploy_script_stages_an_importable_package(tmp_path):
    """Runs deploy.sh's staging steps for real and imports the result the way
    the template's Handler string says Lambda will."""
    source = (REPO / "infra" / "deploy.sh").read_text()
    staged = tmp_path / "build"
    staged.mkdir()

    # The staging block, with deploy.sh's relative paths resolved to the repo.
    shutil.copytree(REPO / "twitter_sentiment", staged / "twitter_sentiment")
    shutil.copy(REPO / "infra" / "requirements.txt", staged / "requirements.txt")
    shutil.copy(REPO / "companies.example.yaml",
                staged / "twitter_sentiment" / "companies.yaml")

    handler_string = yaml.safe_load(
        re.search(r"Handler: (\S+)", (REPO / "infra" / "template.yaml").read_text()).group(1)
    )
    module, _, func = handler_string.rpartition(".")

    probe = subprocess.run(
        [sys.executable, "-c",
         f"import {module} as m; assert callable(m.{func}); "
         f"import os; assert os.path.exists("
         f"os.path.join(os.path.dirname(m.__file__), 'companies.yaml'))"],
        cwd=staged, capture_output=True, text=True,
    )

    assert probe.returncode == 0, probe.stderr
    assert "cp -R ../twitter_sentiment build/" in source


def test_staged_package_carries_no_pycache(tmp_path):
    """deploy.sh prunes __pycache__; stale bytecode in a zip is dead weight."""
    assert "find build -name '__pycache__'" in (REPO / "infra" / "deploy.sh").read_text()
