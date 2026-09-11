"""Persistence: an append-only JSONL result log and a small run-state file.

Two separate concerns, deliberately in two files on disk:

* **Results** (``sentiment.jsonl``) are append-only, one scored post per line,
  so a long history can be tailed, grepped or loaded into pandas without this
  code being involved.
* **State** (``sentiment_state.json``) is the bookkeeping needed to make the
  *next* run cheap and non-duplicative: a per-company ``since_id`` high-water
  mark, and a bounded ring of recent dedupe keys.

``since_id`` works because X post IDs are snowflakes — monotonically increasing
over time — so the largest ID seen is a valid "everything before this is
already handled" marker. The dedupe ring catches the other case: the same
copypasta text reposted under a *new* ID, which ``since_id`` can't filter.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections import deque
from pathlib import Path
from typing import Iterable, Protocol, runtime_checkable

# Recent dedupe keys kept per company. ~5k covers several days of a busy
# company at a few hundred KB of state; beyond that, older copypasta
# resurfacing is rare enough not to be worth the file size.
DEFAULT_SEEN_LIMIT = 5000


@runtime_checkable
class ResultSink(Protocol):
    """Where scored posts go. Local JSONL and S3 both satisfy this."""

    def append(self, records: Iterable[dict]) -> int:
        """Persist these records; returns how many were written."""


@runtime_checkable
class StateStore(Protocol):
    """Per-company scrape bookkeeping. Local JSON and DynamoDB both satisfy this.

    The contract is deliberately small so the pipeline never learns which
    backend it has: read a high-water mark, move it forward, ask whether a
    fingerprint has been seen, record one, and flush.
    """

    def since_id(self, company: str) -> str | None:
        """Highest post ID already handled for this company, if any."""

    def advance(self, company: str, newest_id: str | None) -> None:
        """Move the high-water mark forward. Must never move it backward."""

    def is_duplicate(self, company: str, dedupe_key: str) -> bool:
        """True if this fingerprint was recorded recently."""

    def remember(self, company: str, dedupe_key: str) -> None:
        """Record a fingerprint as seen."""

    def reset(self, company: str) -> None:
        """Forget the high-water mark, keeping the fingerprint history."""

    def save(self) -> None:
        """Flush any buffered changes to the backing store."""


def _atomic_write(path: Path, text: str) -> None:
    """Write via a temp file + rename so a crash can't truncate the state."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def id_max(left: str, right: str) -> str:
    """Larger of two snowflake IDs, compared numerically not lexically.

    Lexical comparison is wrong here: "9" sorts above "10".
    """
    if not left:
        return right
    if not right:
        return left
    try:
        return left if int(left) >= int(right) else right
    except ValueError:
        return max(left, right)


class ResultStore:
    """Append-only JSONL sink for scored posts."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def append(self, records: Iterable[dict]) -> int:
        records = list(records)
        if not records:
            return 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return len(records)

    def read_all(self) -> list[dict]:
        """Load every record back. Malformed lines are skipped, not fatal —
        a half-written line from a killed run shouldn't poison a re-read."""
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out


class RunState:
    """Per-company high-water marks and a bounded recent-dedupe ring."""

    def __init__(self, path: str | Path, seen_limit: int = DEFAULT_SEEN_LIMIT):
        self.path = Path(path)
        self.seen_limit = seen_limit
        self._since: dict[str, str] = {}
        self._seen: dict[str, deque[str]] = {}
        self._seen_set: dict[str, set[str]] = {}
        self.load()

    def _key(self, company: str) -> str:
        return company.strip().lower()

    def load(self) -> None:
        """Read state from disk. A corrupt file resets rather than crashes —
        the cost is one duplicated run, not a dead scraper."""
        self._since, self._seen, self._seen_set = {}, {}, {}
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        if not isinstance(raw, dict):
            return

        for company, entry in (raw.get("companies") or {}).items():
            if not isinstance(entry, dict):
                continue
            key = self._key(company)
            self._since[key] = str(entry.get("since_id") or "")
            keys = [str(k) for k in (entry.get("seen") or [])][-self.seen_limit:]
            self._seen[key] = deque(keys, maxlen=self.seen_limit)
            self._seen_set[key] = set(keys)

    def save(self) -> None:
        payload = {
            "version": 1,
            "companies": {
                company: {
                    "since_id": self._since.get(company, ""),
                    "seen": list(self._seen.get(company, ())),
                }
                for company in set(self._since) | set(self._seen)
            },
        }
        _atomic_write(self.path, json.dumps(payload, ensure_ascii=False, indent=2))

    def since_id(self, company: str) -> str | None:
        return self._since.get(self._key(company)) or None

    def advance(self, company: str, newest_id: str | None) -> None:
        """Move the high-water mark forward. Never backward — a page arriving
        out of order must not make the next run re-fetch old posts."""
        if not newest_id:
            return
        key = self._key(company)
        self._since[key] = id_max(self._since.get(key, ""), str(newest_id))

    def reset(self, company: str) -> None:
        """Forget the high-water mark so the next run re-scans the full window.

        The dedupe ring is kept: re-scanning should re-read posts, not re-emit
        copypasta that was already logged.
        """
        self._since.pop(self._key(company), None)

    def is_duplicate(self, company: str, dedupe_key: str) -> bool:
        return bool(dedupe_key) and dedupe_key in self._seen_set.get(self._key(company), ())

    def remember(self, company: str, dedupe_key: str) -> None:
        if not dedupe_key:
            return
        key = self._key(company)
        ring = self._seen.setdefault(key, deque(maxlen=self.seen_limit))
        seen = self._seen_set.setdefault(key, set())
        if dedupe_key in seen:
            return
        if len(ring) == ring.maxlen:
            seen.discard(ring[0])  # evicted by the append below
        ring.append(dedupe_key)
        seen.add(dedupe_key)
