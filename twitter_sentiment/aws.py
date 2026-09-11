"""AWS-backed implementations of the storage seams, for the Lambda deployment.

The local file stores in :mod:`twitter_sentiment.store` are wrong on Lambda:
the only writable path is ``/tmp``, and it does not survive a cold start. These
satisfy the same :class:`~twitter_sentiment.store.ResultSink` and
:class:`~twitter_sentiment.store.StateStore` protocols, so the pipeline is
unchanged — only the wiring in the handler differs.

Backend choices follow what ``platform/`` already established in this repo:
a single DynamoDB table keyed on ``pk``/``sk``, PAY_PER_REQUEST.

* **Results -> S3.** Append-only, and S3 objects are immutable, so each run
  writes one new object under a date partition rather than rewriting anything.
  The layout is Hive-style (``company=…/dt=…``) so Athena or Glue can read the
  bucket as a partitioned table with no transformation step.
* **State -> DynamoDB.** Small, read-modify-write per company per run, needs
  strong consistency between the read and the write. That is exactly a
  key-value item, not an S3 object.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import uuid
from collections import deque
from typing import Iterable

import boto3
from botocore.exceptions import ClientError

from .store import DEFAULT_SEEN_LIMIT, id_max

# DynamoDB caps an item at 400KB. The dedupe ring is the only unbounded
# attribute, so it gets a tighter default here than the local store uses:
# 2000 x 16 hex chars is ~32KB of payload, an order of magnitude inside the
# limit even with attribute-name overhead.
DYNAMO_SEEN_LIMIT = 2000
STATE_SK = "STATE"


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class S3ResultStore:
    """Writes scored posts as date-partitioned JSONL objects.

    Satisfies :class:`~twitter_sentiment.store.ResultSink`. One object per
    ``append`` call per company — S3 has no append operation, and rewriting a
    growing object would be both slower and a lost-update race between
    concurrent runs.
    """

    def __init__(
        self,
        bucket: str,
        prefix: str = "raw",
        client=None,
        clock=_utcnow,
        run_id: str | None = None,
    ) -> None:
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self._s3 = client or boto3.client("s3")
        self._clock = clock
        # Shared across a run so every object from one invocation is
        # identifiable — makes a partial run easy to find and delete.
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self._seq = 0

    def key_for(self, company: str, when: dt.datetime | None = None) -> str:
        """Build a unique object key.

        The sequence number is not decoration: a PUT to an existing key
        replaces it silently, and two appends for one company inside the same
        second would otherwise collide and lose the first batch. The timestamp
        gives ordering, ``run_id`` separates concurrent invocations, and the
        sequence separates writes within one invocation.
        """
        when = when or self._clock()
        safe = self.partition_name(company)
        self._seq += 1
        return (
            f"{self.prefix}/company={safe}/dt={when:%Y-%m-%d}/"
            f"{when:%H%M%S}-{self.run_id}-{self._seq:04d}.jsonl"
        )

    @staticmethod
    def partition_name(company: str) -> str:
        return company.strip().lower().replace("/", "-").replace(" ", "-")

    def append(self, records: Iterable[dict]) -> int:
        records = list(records)
        if not records:
            return 0

        written = 0
        # Defensive: the pipeline calls this once per company, but a caller
        # that batches companies together must not silently land in the wrong
        # partition.
        by_company: dict[str, list[dict]] = {}
        for record in records:
            by_company.setdefault(record.get("company", "unknown"), []).append(record)

        for company, rows in by_company.items():
            body = "".join(
                json.dumps(r, ensure_ascii=False) + "\n" for r in rows
            ).encode("utf-8")
            self._s3.put_object(
                Bucket=self.bucket,
                Key=self.key_for(company),
                Body=body,
                ContentType="application/x-ndjson",
            )
            written += len(rows)
        return written

    def read_all(self, company: str | None = None) -> list[dict]:
        """Read every record back. For debugging and tests — at real volume
        query the bucket with Athena instead of pulling it through here."""
        prefix = self.prefix
        if company:
            prefix = f"{prefix}/company={self.partition_name(company)}/"

        out: list[dict] = []
        paginator = self._s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for obj in sorted(page.get("Contents", []), key=lambda o: o["Key"]):
                body = self._s3.get_object(Bucket=self.bucket, Key=obj["Key"])
                for line in body["Body"].read().decode("utf-8").splitlines():
                    if line.strip():
                        out.append(json.loads(line))
        return out


class DynamoRunState:
    """Per-company ``since_id`` and dedupe ring in a DynamoDB table.

    Satisfies :class:`~twitter_sentiment.store.StateStore`. Items are keyed
    ``pk=SENTIMENT#<company>``, ``sk=STATE``, matching the single-table
    convention ``platform/`` already uses.

    Companies are loaded lazily on first touch rather than scanned up front:
    a run only reads the handful of companies it is actually scraping, and a
    table shared with other item types is never scanned at all.
    """

    def __init__(
        self,
        table_name: str,
        resource=None,
        seen_limit: int = DYNAMO_SEEN_LIMIT,
        clock=_utcnow,
    ) -> None:
        self.table_name = table_name
        self.seen_limit = seen_limit
        self._table = (resource or boto3.resource("dynamodb")).Table(table_name)
        self._clock = clock
        self._since: dict[str, str] = {}
        self._seen: dict[str, deque[str]] = {}
        self._seen_set: dict[str, set[str]] = {}
        self._loaded: set[str] = set()
        self._dirty: set[str] = set()

    @staticmethod
    def _key(company: str) -> str:
        return company.strip().lower()

    @staticmethod
    def pk_for(company: str) -> str:
        return f"SENTIMENT#{company.strip().lower()}"

    def _load(self, company: str) -> str:
        key = self._key(company)
        if key in self._loaded:
            return key
        self._loaded.add(key)

        try:
            item = self._table.get_item(
                Key={"pk": self.pk_for(company), "sk": STATE_SK},
                ConsistentRead=True,
            ).get("Item")
        except ClientError:
            # A read failure must not be mistaken for "no state" — that would
            # silently re-scrape and re-emit the whole window. Fail the run.
            raise

        if item:
            self._since[key] = str(item.get("since_id") or "")
            recent = [str(k) for k in (item.get("seen") or [])][-self.seen_limit:]
            self._seen[key] = deque(recent, maxlen=self.seen_limit)
            self._seen_set[key] = set(recent)
        else:
            self._seen[key] = deque(maxlen=self.seen_limit)
            self._seen_set[key] = set()
        return key

    def since_id(self, company: str) -> str | None:
        return self._since.get(self._load(company)) or None

    def advance(self, company: str, newest_id: str | None) -> None:
        if not newest_id:
            return
        key = self._load(company)
        merged = id_max(self._since.get(key, ""), str(newest_id))
        if merged != self._since.get(key, ""):
            self._since[key] = merged
            self._dirty.add(key)

    def is_duplicate(self, company: str, dedupe_key: str) -> bool:
        if not dedupe_key:
            return False
        return dedupe_key in self._seen_set.get(self._load(company), ())

    def remember(self, company: str, dedupe_key: str) -> None:
        if not dedupe_key:
            return
        key = self._load(company)
        ring, seen = self._seen[key], self._seen_set[key]
        if dedupe_key in seen:
            return
        if len(ring) == ring.maxlen:
            seen.discard(ring[0])  # evicted by the append below
        ring.append(dedupe_key)
        seen.add(dedupe_key)
        self._dirty.add(key)

    def reset(self, company: str) -> None:
        key = self._load(company)
        if self._since.pop(key, None) is not None:
            self._dirty.add(key)

    def save(self) -> None:
        """Write back only the companies this run actually changed."""
        for key in sorted(self._dirty):
            self._table.put_item(Item={
                "pk": f"SENTIMENT#{key}",
                "sk": STATE_SK,
                "company": key,
                "since_id": self._since.get(key, ""),
                "seen": list(self._seen.get(key, ())),
                "updated_at": self._clock().isoformat(),
            })
        self._dirty.clear()


def resolve_bearer_token(env: dict | None = None, secrets_client=None,
                         ssm_client=None) -> str:
    """Find the X bearer token, preferring the least-bad place to keep it.

    Order: a Secrets Manager secret, then an SSM SecureString, then a plain
    environment variable. The env var is last on purpose — Lambda environment
    variables are visible to anyone with ``lambda:GetFunctionConfiguration``
    and show up in the console, so it is the fallback for local runs, not the
    deployed path.
    """
    env = os.environ if env is None else env

    secret_id = (env.get("X_BEARER_SECRET_ID") or "").strip()
    if secret_id:
        client = secrets_client or boto3.client("secretsmanager")
        value = client.get_secret_value(SecretId=secret_id)["SecretString"]
        return _unwrap_secret(value)

    param = (env.get("X_BEARER_SSM_PARAM") or "").strip()
    if param:
        client = ssm_client or boto3.client("ssm")
        return client.get_parameter(
            Name=param, WithDecryption=True
        )["Parameter"]["Value"].strip()

    token = (env.get("X_BEARER_TOKEN") or env.get("TWITTER_BEARER_TOKEN") or "").strip()
    if not token:
        raise RuntimeError(
            "no X bearer token: set X_BEARER_SECRET_ID (Secrets Manager), "
            "X_BEARER_SSM_PARAM (SSM SecureString), or X_BEARER_TOKEN"
        )
    return token


def _unwrap_secret(value: str) -> str:
    """Accept either a raw token or a JSON blob holding one.

    The console's "key/value" secret editor produces JSON, the "plaintext"
    editor does not, and which one was used is not worth being a deploy-time
    footgun.
    """
    value = value.strip()
    if value.startswith("{"):
        try:
            payload = json.loads(value)
        except json.JSONDecodeError:
            return value
        for field in ("X_BEARER_TOKEN", "bearer_token", "token", "value"):
            if payload.get(field):
                return str(payload[field]).strip()
        raise RuntimeError(
            "secret JSON has no X_BEARER_TOKEN/bearer_token/token/value field"
        )
    return value
