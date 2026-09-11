"""AWS backend tests, against moto's emulated S3 / DynamoDB / Secrets Manager.

Emulation rather than stubs: the things worth testing here are real service
behaviours — S3 object immutability, DynamoDB consistent reads, the shape a
secret actually comes back in — and a hand-rolled stub would just encode my
assumptions about those instead of checking them.
"""

import datetime as dt
import json

import boto3
import pytest
from moto import mock_aws

from twitter_sentiment.aws import (
    DYNAMO_SEEN_LIMIT,
    DynamoRunState,
    S3ResultStore,
    resolve_bearer_token,
)
from twitter_sentiment.store import ResultSink, RunState, StateStore

REGION = "us-east-1"
BUCKET = "sentiment-test"
TABLE = "sentiment-test-table"
UTC = dt.timezone.utc


@pytest.fixture
def aws(monkeypatch):
    """Credentials that are unmistakably fake, so a leak can't hit real AWS."""
    for key, value in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "AWS_SECURITY_TOKEN": "testing",
        "AWS_DEFAULT_REGION": REGION,
    }.items():
        monkeypatch.setenv(key, value)
    with mock_aws():
        yield


@pytest.fixture
def bucket(aws):
    boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)
    return BUCKET


@pytest.fixture
def table(aws):
    boto3.client("dynamodb", region_name=REGION).create_table(
        TableName=TABLE,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
        ],
        KeySchema=[
            {"AttributeName": "pk", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
    )
    return TABLE


def record(post_id, company="Tesla", label="positive"):
    return {"id": str(post_id), "company": company, "label": label, "text": "x"}


# --------------------------------------------------------------------------
# Both backends must satisfy the protocols the pipeline depends on.
# --------------------------------------------------------------------------

def test_both_result_sinks_satisfy_the_protocol(tmp_path, bucket):
    from twitter_sentiment.store import ResultStore

    assert isinstance(ResultStore(tmp_path / "o.jsonl"), ResultSink)
    assert isinstance(S3ResultStore(bucket), ResultSink)


def test_both_state_stores_satisfy_the_protocol(tmp_path, table):
    assert isinstance(RunState(tmp_path / "s.json"), StateStore)
    assert isinstance(DynamoRunState(table), StateStore)


# --------------------------------------------------------------------------
# S3
# --------------------------------------------------------------------------

def test_append_writes_partitioned_ndjson(bucket):
    store = S3ResultStore(bucket, clock=lambda: dt.datetime(2026, 9, 11, 4, 43, 7, tzinfo=UTC))

    assert store.append([record(1), record(2)]) == 2

    keys = [o["Key"] for o in boto3.client("s3", region_name=REGION)
            .list_objects_v2(Bucket=bucket)["Contents"]]
    assert len(keys) == 1
    assert keys[0] == "raw/company=tesla/dt=2026-09-11/044307-" + store.run_id + "-0001.jsonl"


def test_object_is_readable_ndjson_with_the_right_content_type(bucket):
    store = S3ResultStore(bucket)
    store.append([record(1), record(2)])

    s3 = boto3.client("s3", region_name=REGION)
    key = s3.list_objects_v2(Bucket=bucket)["Contents"][0]["Key"]
    obj = s3.get_object(Bucket=bucket, Key=key)

    assert obj["ContentType"] == "application/x-ndjson"
    lines = obj["Body"].read().decode().strip().split("\n")
    assert [json.loads(l)["id"] for l in lines] == ["1", "2"]


def test_each_append_writes_a_new_object_rather_than_rewriting(bucket):
    """A PUT to an existing key replaces it, so same-second appends must not
    collide — otherwise the first batch is lost with no error anywhere."""
    frozen = dt.datetime(2026, 9, 11, 4, 43, 7, tzinfo=UTC)
    store = S3ResultStore(bucket, clock=lambda: frozen)
    store.append([record(1)])
    store.append([record(2)])

    objects = boto3.client("s3", region_name=REGION).list_objects_v2(Bucket=bucket)
    assert objects["KeyCount"] == 2
    assert [r["id"] for r in store.read_all()] == ["1", "2"]


def test_mixed_companies_land_in_separate_partitions(bucket):
    store = S3ResultStore(bucket)
    store.append([record(1, company="Tesla"), record(2, company="Rivian")])

    keys = sorted(o["Key"] for o in boto3.client("s3", region_name=REGION)
                  .list_objects_v2(Bucket=bucket)["Contents"])
    assert "company=rivian" in keys[0] and "company=tesla" in keys[1]


def test_company_names_with_spaces_make_valid_keys(bucket):
    store = S3ResultStore(bucket)
    store.append([record(1, company="Boston Dynamics")])

    key = boto3.client("s3", region_name=REGION).list_objects_v2(
        Bucket=bucket)["Contents"][0]["Key"]
    assert "company=boston-dynamics/" in key


def test_read_all_can_filter_to_one_company(bucket):
    store = S3ResultStore(bucket)
    store.append([record(1, company="Tesla"), record(2, company="Rivian")])

    assert [r["id"] for r in store.read_all(company="Rivian")] == ["2"]


def test_empty_append_writes_nothing(bucket):
    assert S3ResultStore(bucket).append([]) == 0
    assert boto3.client("s3", region_name=REGION).list_objects_v2(
        Bucket=bucket).get("KeyCount", 0) == 0


def test_run_id_is_shared_across_a_run_and_stable(bucket):
    store = S3ResultStore(bucket, run_id="abc123")
    store.append([record(1)])
    store.append([record(2)])

    keys = [o["Key"] for o in boto3.client("s3", region_name=REGION)
            .list_objects_v2(Bucket=bucket)["Contents"]]
    assert all("abc123" in k for k in keys)


def test_non_ascii_survives_the_round_trip(bucket):
    store = S3ResultStore(bucket)
    store.append([{"company": "Nestle", "id": "1", "text": "Nestlé 🔥"}])

    assert store.read_all()[0]["text"] == "Nestlé 🔥"


# --------------------------------------------------------------------------
# DynamoDB
# --------------------------------------------------------------------------

def test_since_id_round_trips_through_the_table(table):
    state = DynamoRunState(table)
    state.advance("Tesla", "1800")
    state.save()

    assert DynamoRunState(table).since_id("Tesla") == "1800"


def test_item_uses_the_repo_single_table_key_convention(table):
    state = DynamoRunState(table)
    state.advance("Tesla", "1800")
    state.save()

    item = boto3.resource("dynamodb", region_name=REGION).Table(table).get_item(
        Key={"pk": "SENTIMENT#tesla", "sk": "STATE"})["Item"]

    assert item["since_id"] == "1800"
    assert item["company"] == "tesla"
    assert item["updated_at"]


def test_advance_never_moves_backward(table):
    state = DynamoRunState(table)
    state.advance("Tesla", "1800")
    state.advance("Tesla", "1700")
    state.save()

    assert DynamoRunState(table).since_id("Tesla") == "1800"


def test_snowflakes_compare_numerically_not_lexically(table):
    state = DynamoRunState(table)
    state.advance("Tesla", "9")
    state.advance("Tesla", "10")

    assert state.since_id("Tesla") == "10"


def test_missing_company_has_no_mark(table):
    assert DynamoRunState(table).since_id("Nobody") is None


def test_company_lookup_is_case_insensitive(table):
    state = DynamoRunState(table)
    state.advance("Tesla", "1800")

    assert state.since_id("TESLA") == "1800"


def test_dedupe_ring_round_trips(table):
    state = DynamoRunState(table)
    state.remember("Tesla", "abcd1234")
    state.save()

    reloaded = DynamoRunState(table)
    assert reloaded.is_duplicate("Tesla", "abcd1234")
    assert not reloaded.is_duplicate("Tesla", "ffff0000")
    assert not reloaded.is_duplicate("Rivian", "abcd1234")


def test_dedupe_ring_evicts_oldest_past_the_limit(table):
    state = DynamoRunState(table, seen_limit=3)
    for key in ["a", "b", "c", "d"]:
        state.remember("Tesla", key)

    assert not state.is_duplicate("Tesla", "a")
    assert all(state.is_duplicate("Tesla", k) for k in ["b", "c", "d"])


def test_ring_limit_is_applied_on_load(table):
    boto3.resource("dynamodb", region_name=REGION).Table(table).put_item(Item={
        "pk": "SENTIMENT#tesla", "sk": "STATE",
        "since_id": "1", "seen": ["a", "b", "c", "d", "e"],
    })
    state = DynamoRunState(table, seen_limit=2)

    assert not state.is_duplicate("Tesla", "a")
    assert state.is_duplicate("Tesla", "e")


def test_empty_dedupe_keys_are_ignored(table):
    state = DynamoRunState(table)
    state.remember("Tesla", "")

    assert not state.is_duplicate("Tesla", "")


def test_reset_clears_the_mark_but_keeps_dedupe(table):
    state = DynamoRunState(table)
    state.advance("Tesla", "1800")
    state.remember("Tesla", "abcd")
    state.reset("Tesla")
    state.save()

    reloaded = DynamoRunState(table)
    assert reloaded.since_id("Tesla") is None
    assert reloaded.is_duplicate("Tesla", "abcd")


def test_save_writes_only_changed_companies(table):
    state = DynamoRunState(table)
    state.advance("Tesla", "1800")
    state.since_id("Rivian")      # read-only touch
    state.save()

    ddb = boto3.resource("dynamodb", region_name=REGION).Table(table)
    assert "Item" in ddb.get_item(Key={"pk": "SENTIMENT#tesla", "sk": "STATE"})
    assert "Item" not in ddb.get_item(Key={"pk": "SENTIMENT#rivian", "sk": "STATE"})


def test_repeated_save_is_idempotent(table):
    state = DynamoRunState(table)
    state.advance("Tesla", "1800")
    state.save()
    state.save()

    assert DynamoRunState(table).since_id("Tesla") == "1800"


def test_unrelated_items_in_a_shared_table_are_untouched(table):
    """The repo's single-table convention means sharing with job-watcher rows."""
    ddb = boto3.resource("dynamodb", region_name=REGION).Table(table)
    ddb.put_item(Item={"pk": "JOB#123", "sk": "STATE", "status": "waiting"})

    state = DynamoRunState(table)
    state.advance("Tesla", "1800")
    state.save()

    assert ddb.get_item(Key={"pk": "JOB#123", "sk": "STATE"})["Item"]["status"] == "waiting"


def test_default_ring_stays_well_inside_the_dynamo_item_limit(table):
    """400KB is a hard DynamoDB limit; blowing it fails the run, not the item."""
    state = DynamoRunState(table)
    for i in range(DYNAMO_SEEN_LIMIT):
        state.remember("Tesla", f"{i:016x}")
    state.advance("Tesla", "1800")
    state.save()

    item = boto3.resource("dynamodb", region_name=REGION).Table(table).get_item(
        Key={"pk": "SENTIMENT#tesla", "sk": "STATE"})["Item"]
    assert len(item["seen"]) == DYNAMO_SEEN_LIMIT
    assert len(json.dumps(item, default=str).encode()) < 200_000


# --------------------------------------------------------------------------
# Secret resolution
# --------------------------------------------------------------------------

def test_plaintext_secrets_manager_secret(aws):
    boto3.client("secretsmanager", region_name=REGION).create_secret(
        Name="x-token", SecretString="plain-token")

    assert resolve_bearer_token({"X_BEARER_SECRET_ID": "x-token"}) == "plain-token"


def test_json_secrets_manager_secret(aws):
    boto3.client("secretsmanager", region_name=REGION).create_secret(
        Name="x-token-json", SecretString=json.dumps({"X_BEARER_TOKEN": "json-token"}))

    assert resolve_bearer_token({"X_BEARER_SECRET_ID": "x-token-json"}) == "json-token"


def test_json_secret_without_a_known_field_is_an_error(aws):
    boto3.client("secretsmanager", region_name=REGION).create_secret(
        Name="x-bad", SecretString=json.dumps({"unrelated": "x"}))

    with pytest.raises(RuntimeError, match="no X_BEARER_TOKEN"):
        resolve_bearer_token({"X_BEARER_SECRET_ID": "x-bad"})


def test_ssm_securestring(aws):
    boto3.client("ssm", region_name=REGION).put_parameter(
        Name="/sentiment/x-token", Value="ssm-token", Type="SecureString")

    assert resolve_bearer_token({"X_BEARER_SSM_PARAM": "/sentiment/x-token"}) == "ssm-token"


def test_env_var_is_the_fallback(aws):
    assert resolve_bearer_token({"X_BEARER_TOKEN": "env-token"}) == "env-token"


def test_secrets_manager_wins_over_the_env_var(aws):
    boto3.client("secretsmanager", region_name=REGION).create_secret(
        Name="x-token", SecretString="secret-token")

    assert resolve_bearer_token({
        "X_BEARER_SECRET_ID": "x-token", "X_BEARER_TOKEN": "env-token",
    }) == "secret-token"


def test_no_token_anywhere_is_actionable(aws):
    with pytest.raises(RuntimeError, match="X_BEARER_SECRET_ID"):
        resolve_bearer_token({})
