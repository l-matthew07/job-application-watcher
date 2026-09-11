"""Lambda entrypoint: run the scrape on a schedule, in AWS.

EventBridge invokes this on a fixed rate. It reads its company list, scrapes
each one, writes scored posts to S3 and bookkeeping to DynamoDB, and emits
CloudWatch metrics so the run is observable without reading logs.

Three things here exist because Lambda is not a laptop:

* **The scorer is cached across invocations.** Loading VADER's 7.5k-word
  lexicon takes about a second — paying that on every scheduled run, forever,
  is pure waste. Module-level state survives a warm container.
* **State is saved in a ``finally``.** A crash scraping the fourth company
  must not discard the high-water marks earned by the first three, or the next
  run re-fetches and re-emits everything.
* **The run stops before the timeout.** Being killed mid-scrape skips the save
  entirely, so the loop checks remaining time and stops cleanly instead.
"""

from __future__ import annotations

import json
import os
import time

from .aws import DynamoRunState, S3ResultStore, resolve_bearer_token
from .client import XApiError, XSearchClient
from .config import ConfigError, ScrapeConfig, load_config, parse_config
from .pipeline import CompanyRun, run_company
from .sentiment import SentimentScorer

DEFAULT_NAMESPACE = "TwitterSentiment"
# Stop starting a new company with less than this left. A company is up to
# max_pages API calls plus scoring; 45s is comfortably more than one takes.
SAFETY_MARGIN_MS = 45_000

_scorer: SentimentScorer | None = None
_scorer_key: tuple | None = None


def _get_scorer(config: ScrapeConfig) -> SentimentScorer:
    """Build the scorer once per container, rebuilding only if the lexicon
    config actually changed (it can, when the config lives in S3)."""
    global _scorer, _scorer_key
    key = (
        tuple(sorted(config.extra_lexicon.items())),
        tuple(sorted(config.extra_phrases.items())),
    )
    if _scorer is None or _scorer_key != key:
        _scorer = SentimentScorer(
            extra_lexicon=config.extra_lexicon, extra_phrases=config.extra_phrases
        )
        _scorer_key = key
    return _scorer


def load_run_config(env: dict | None = None, s3_client=None) -> ScrapeConfig:
    """Resolve the company list: S3 first, then a file baked into the package.

    S3 first so the watched companies can change without a redeploy — the
    bundled file is the fallback and the bootstrap default.
    """
    env = os.environ if env is None else env

    uri = (env.get("CONFIG_S3_URI") or "").strip()
    if uri:
        if not uri.startswith("s3://"):
            raise ConfigError(f"CONFIG_S3_URI must be an s3:// URI, got {uri!r}")
        bucket, _, key = uri[len("s3://"):].partition("/")
        if not bucket or not key:
            raise ConfigError(f"CONFIG_S3_URI is missing a bucket or key: {uri!r}")
        import boto3

        client = s3_client or boto3.client("s3")
        body = client.get_object(Bucket=bucket, Key=key)["Body"].read()
        return parse_config(body.decode("utf-8"), source=uri)

    path = env.get("CONFIG_PATH") or os.path.join(
        os.path.dirname(__file__), "companies.yaml"
    )
    return load_config(path)


def emit_metrics(run: CompanyRun, namespace: str = DEFAULT_NAMESPACE,
                 clock=time.time, out=print) -> dict:
    """Emit one CloudWatch Embedded Metric Format log line for a company.

    EMF rather than PutMetricData: CloudWatch extracts the metrics from the
    log line the function already pays to write, so there's no extra API call,
    no added latency on the critical path, and nothing to fail separately.
    """
    summary = run.summary
    payload = {
        "_aws": {
            "Timestamp": int(clock() * 1000),
            "CloudWatchMetrics": [{
                "Namespace": namespace,
                "Dimensions": [["Company"]],
                "Metrics": [
                    {"Name": "PostsFetched", "Unit": "Count"},
                    {"Name": "PostsKept", "Unit": "Count"},
                    {"Name": "PostsDropped", "Unit": "Count"},
                    {"Name": "NetSentiment", "Unit": "None"},
                    {"Name": "MeanCompound", "Unit": "None"},
                    {"Name": "WeightedCompound", "Unit": "None"},
                    {"Name": "TotalEngagement", "Unit": "Count"},
                ],
            }],
        },
        "Company": run.company,
        "PostsFetched": run.fetched,
        "PostsKept": run.kept,
        "PostsDropped": sum(run.drops.values()),
        "NetSentiment": round(summary.net_sentiment, 4),
        "MeanCompound": round(summary.mean_compound, 4),
        "WeightedCompound": round(summary.weighted_compound, 4),
        "TotalEngagement": summary.total_engagement,
        # Not a metric — a searchable log field, so a volume drop can be
        # explained from the same line that reported it.
        "drops": run.drops,
    }
    out(json.dumps(payload))
    return payload


def _remaining_ms(context) -> float:
    getter = getattr(context, "get_remaining_time_in_millis", None)
    return getter() if callable(getter) else float("inf")


def lambda_handler(event, context=None):
    """EventBridge target. Returns a per-company summary for the run record."""
    env = os.environ
    config = load_run_config(env)

    bucket = env["RESULTS_BUCKET"]
    table = env["STATE_TABLE"]
    namespace = env.get("METRICS_NAMESPACE", DEFAULT_NAMESPACE)

    only = {c.strip().lower() for c in (event or {}).get("companies", []) if c.strip()}
    companies = [c for c in config.companies if not only or c.name.lower() in only]

    scorer = _get_scorer(config)
    client = XSearchClient(resolve_bearer_token(env))
    store = S3ResultStore(bucket, prefix=env.get("RESULTS_PREFIX", "raw"))
    state = DynamoRunState(table)

    results, skipped, failed = [], [], None
    try:
        for company in companies:
            if _remaining_ms(context) < SAFETY_MARGIN_MS:
                # Out of time. Stopping here lets the finally-block save; being
                # killed mid-company would lose every mark earned this run.
                skipped = [c.name for c in companies[len(results) + len(skipped):]]
                print(json.dumps({
                    "level": "WARN",
                    "message": "stopping early, out of time",
                    "skipped": skipped,
                }))
                break

            run = run_company(client, scorer, company, state)
            store.append(p.to_record() for p in run.scored)
            emit_metrics(run, namespace)
            results.append(run)
    except XApiError as exc:
        # Let the invocation fail so EventBridge/the DLQ see it, but not before
        # the finally-block banks the companies that did succeed.
        failed = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        state.save()
        print(json.dumps({
            "level": "ERROR" if failed else "INFO",
            "message": "run finished",
            "run_id": store.run_id,
            "companies_scraped": len(results),
            "companies_skipped": skipped,
            "api_requests": client.requests_made,
            "error": failed,
        }))

    return {
        "run_id": store.run_id,
        "api_requests": client.requests_made,
        "skipped": skipped,
        "companies": [
            {
                "company": r.company,
                "fetched": r.fetched,
                "kept": r.kept,
                "drops": r.drops,
                "net_sentiment": round(r.summary.net_sentiment, 4),
                "mean_compound": round(r.summary.mean_compound, 4),
            }
            for r in results
        ],
    }
