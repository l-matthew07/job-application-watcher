"""Tests over the SAM template itself.

These exist because the template and the code fail apart silently: rename an
environment variable in the handler and the stack still deploys, still runs on
schedule, and every invocation raises KeyError at 3am. Nothing short of a test
that reads both catches that at commit time.
"""

import pathlib
import re

import pytest
import yaml

TEMPLATE_PATH = pathlib.Path(__file__).resolve().parents[1] / "infra" / "template.yaml"
HANDLER_PATH = (
    pathlib.Path(__file__).resolve().parents[1] / "twitter_sentiment" / "handler.py"
)
AWS_PATH = pathlib.Path(__file__).resolve().parents[1] / "twitter_sentiment" / "aws.py"
DEPLOY_PATH = pathlib.Path(__file__).resolve().parents[1] / "infra" / "deploy.sh"


class CfnLoader(yaml.SafeLoader):
    """CloudFormation short forms (!Ref, !Sub, !GetAtt) aren't plain YAML."""


CfnLoader.add_multi_constructor(
    "!", lambda loader, suffix, node: {f"Fn::{suffix}": _node_value(loader, node)}
)


def _node_value(loader, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_mapping(node, deep=True)


@pytest.fixture(scope="module")
def template():
    return yaml.load(TEMPLATE_PATH.read_text(), Loader=CfnLoader)


@pytest.fixture(scope="module")
def function(template):
    return template["Resources"]["ScraperFunction"]["Properties"]


def env_names(function):
    return set(function["Environment"]["Variables"])


# --------------------------------------------------------------------------
# The template and the code must agree
# --------------------------------------------------------------------------

def test_every_env_var_the_handler_requires_is_supplied(function):
    """os.environ["X"] raises at runtime; the stack deploys happily without it."""
    required = set(re.findall(r'env\["([A-Z_]+)"\]', HANDLER_PATH.read_text()))

    assert required, "expected the handler to read some required env vars"
    assert required <= env_names(function), required - env_names(function)


def test_optional_env_vars_the_code_reads_are_known_to_the_template(function):
    """Guards the reverse drift: a var the code reads that nobody supplies."""
    source = HANDLER_PATH.read_text() + AWS_PATH.read_text()
    read = set(re.findall(r'env\.get\("([A-Z_]+)"', source))
    # Local/CLI-only fallbacks that the deployed stack deliberately omits.
    local_only = {"X_BEARER_TOKEN", "TWITTER_BEARER_TOKEN", "X_BEARER_SSM_PARAM",
                  "CONFIG_PATH", "AWS_REGION"}

    assert (read - local_only) <= env_names(function), read - local_only - env_names(function)


def test_handler_path_matches_the_real_module(function):
    module, _, func = function["Handler"].rpartition(".")
    imported = __import__(module, fromlist=[func])

    assert callable(getattr(imported, func))


def test_secret_env_var_points_at_the_secret_parameter(function):
    assert function["Environment"]["Variables"]["X_BEARER_SECRET_ID"] == {
        "Fn::Ref": "BearerTokenSecretArn"
    }


def test_runtime_requirements_cover_the_package_imports():
    """boto3 is intentionally absent — the runtime provides it."""
    lines = [
        line.strip().lower()
        for line in (TEMPLATE_PATH.parent / "requirements.txt").read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]

    for package in ("requests", "pyyaml", "vadersentiment"):
        assert any(line.startswith(package) for line in lines), package
    assert not any(line.startswith("boto3") for line in lines)


# --------------------------------------------------------------------------
# Operational properties worth not regressing
# --------------------------------------------------------------------------

def test_scraper_is_limited_to_one_concurrent_run(function):
    """Two runs would race the DynamoDB read-modify-write and lose a ring."""
    assert function["ReservedConcurrentExecutions"] == 1


def test_scraper_has_a_dead_letter_queue(function):
    assert function["DeadLetterQueue"]["Type"] == "SQS"


def test_scraper_runs_on_a_schedule(function):
    assert function["Events"]["Scheduled"]["Type"] == "Schedule"


def test_stateful_resources_survive_stack_deletion(template):
    """Recent search only reaches back 7 days — deleted history is gone."""
    for name in ("ResultsBucket", "StateTable"):
        assert template["Resources"][name]["DeletionPolicy"] == "Retain"
        assert template["Resources"][name]["UpdateReplacePolicy"] == "Retain"


def test_results_bucket_blocks_public_access_and_encrypts(template):
    props = template["Resources"]["ResultsBucket"]["Properties"]

    assert all(props["PublicAccessBlockConfiguration"].values())
    assert props["BucketEncryption"]


def test_state_table_uses_the_repo_single_table_key_convention(template):
    keys = template["Resources"]["StateTable"]["Properties"]["KeySchema"]

    assert [k["AttributeName"] for k in keys] == ["pk", "sk"]
    assert template["Resources"]["StateTable"]["Properties"]["BillingMode"] == "PAY_PER_REQUEST"


def test_results_prefix_is_shared_by_the_function_and_lifecycle_rule(template, function):
    """A lifecycle rule whose prefix doesn't match the writes ages out nothing."""
    rules = template["Resources"]["ResultsBucket"]["Properties"][
        "LifecycleConfiguration"]["Rules"]
    age_out = next(r for r in rules if r["Id"] == "age-out-raw-posts")

    assert function["Environment"]["Variables"]["RESULTS_PREFIX"] == {
        "Fn::Ref": "ResultsPrefix"
    }
    assert age_out["Prefix"] == {"Fn::Sub": "${ResultsPrefix}/"}


def test_silent_failure_alarm_watches_the_handler_metric(template):
    """The alarm is worthless if it names a metric the handler never emits."""
    from twitter_sentiment.handler import DEFAULT_NAMESPACE, emit_metrics
    from twitter_sentiment.models import CompanySentiment
    from twitter_sentiment.pipeline import CompanyRun

    alarm = template["Resources"]["NoPostsAlarm"]["Properties"]
    emitted = emit_metrics(
        CompanyRun(company="X", summary=CompanySentiment(company="X")),
        out=lambda _: None,
    )

    assert alarm["MetricName"] in emitted
    assert alarm["Namespace"] == {"Fn::Ref": "MetricsNamespace"}
    assert template["Parameters"]["MetricsNamespace"]["Default"] == DEFAULT_NAMESPACE


def test_silent_failure_alarm_fires_on_missing_data(template):
    """Metrics stopping entirely IS the symptom — it must not be ignored."""
    assert template["Resources"]["NoPostsAlarm"]["Properties"]["TreatMissingData"] == "breaching"


def test_every_alarm_notifies_the_topic(template):
    alarms = [
        name for name, body in template["Resources"].items()
        if body["Type"] == "AWS::CloudWatch::Alarm"
    ]

    assert len(alarms) >= 3
    for name in alarms:
        actions = template["Resources"][name]["Properties"]["AlarmActions"]
        assert actions == [{"Fn::Ref": "AlarmTopic"}], name


def test_bearer_token_is_not_a_template_parameter(template):
    """A NoEcho parameter is redacted in the console but still travels through
    CloudFormation — the token belongs in Secrets Manager, by ARN."""
    params = template["Parameters"]

    assert "BearerTokenSecretArn" in params
    assert not any("token" in p.lower() and p != "BearerTokenSecretArn" for p in params)


def test_deploy_script_stages_the_package_codeuri_points_at(function):
    """CodeUri: build/ is only correct if deploy.sh actually builds build/."""
    deploy = DEPLOY_PATH.read_text()

    assert function["CodeUri"] == "build/"
    assert "mkdir -p build" in deploy
    assert "cp -R ../twitter_sentiment build/" in deploy


def test_deploy_script_bundles_a_companies_file(function):
    """The handler falls back to twitter_sentiment/companies.yaml; if deploy.sh
    doesn't put one there, every run fails on a missing config."""
    assert "build/twitter_sentiment/companies.yaml" in DEPLOY_PATH.read_text()
