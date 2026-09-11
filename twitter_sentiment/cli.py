"""Command-line entrypoint: ``python -m twitter_sentiment``.

    # every company in the config
    python -m twitter_sentiment --config companies.yaml

    # one ad-hoc company, no config file
    python -m twitter_sentiment --company Rivian --term rivian --term '$RIVN'

    # see the query without spending an API call
    python -m twitter_sentiment --config companies.yaml --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace

from dotenv import load_dotenv

from .aggregate import summarize
from .client import XApiError, XSearchClient
from .config import ConfigError, bearer_token, config_from_terms, load_config
from .models import CompanySentiment
from .pipeline import CompanyRun, run_company
from .sentiment import SentimentScorer
from .store import ResultStore, RunState

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_API = 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="twitter_sentiment",
        description="Scrape public sentiment about companies from X.",
    )
    source = parser.add_argument_group("what to watch")
    source.add_argument("--config", help="path to a companies YAML file")
    source.add_argument("--company", help="ad-hoc company name (instead of --config)")
    source.add_argument(
        "--term", action="append", default=[], dest="terms",
        help="search term for --company; repeatable. Defaults to the name.",
    )
    source.add_argument(
        "--only", action="append", default=[], dest="only",
        help="limit a --config run to these companies; repeatable",
    )

    tuning = parser.add_argument_group("scrape tuning")
    tuning.add_argument("--max-pages", type=int, help="override pages per company")
    tuning.add_argument("--max-results", type=int, help="override posts per page (10-100)")
    tuning.add_argument("--lang", help="override the language filter")
    tuning.add_argument(
        "--fresh", action="store_true",
        help="ignore the saved since_id high-water mark and re-scan the window",
    )

    output = parser.add_argument_group("output")
    output.add_argument("--out", help="override the JSONL results path")
    output.add_argument("--state", help="override the run-state path")
    output.add_argument("--json", action="store_true", help="print machine-readable JSON")
    output.add_argument("--examples", type=int, default=3, help="example posts per side")
    output.add_argument(
        "--dry-run", action="store_true",
        help="print each company's query and exit without calling the API",
    )
    return parser


def _resolve_config(args):
    if args.config and args.company:
        raise ConfigError("pass --config or --company, not both")
    if args.config:
        config = load_config(args.config)
    elif args.company:
        config = config_from_terms(args.company, args.terms)
    else:
        raise ConfigError("nothing to watch: pass --config or --company")

    companies = config.companies
    if args.only:
        wanted = {name.lower() for name in args.only}
        companies = tuple(c for c in companies if c.name.lower() in wanted)
        missing = wanted - {c.name.lower() for c in companies}
        if missing:
            raise ConfigError(f"--only named unknown companies: {sorted(missing)}")

    overrides = {
        key: value
        for key, value in (
            ("max_pages", args.max_pages),
            ("max_results", args.max_results),
            ("lang", args.lang),
        )
        if value is not None
    }
    if overrides:
        companies = tuple(replace(c, **overrides) for c in companies)
        for company in companies:
            company.build_query()  # re-validate after the override

    return replace(
        config,
        companies=companies,
        output_path=args.out or config.output_path,
        state_path=args.state or config.state_path,
    )


def _run_payload(run: CompanyRun, examples: int) -> dict:
    summary = run.summary
    return {
        "company": run.company,
        "fetched": run.fetched,
        "kept": run.kept,
        "drops": run.drops,
        "total": summary.total,
        "positive": summary.positive,
        "neutral": summary.neutral,
        "negative": summary.negative,
        "net_sentiment": round(summary.net_sentiment, 4),
        "mean_compound": round(summary.mean_compound, 4),
        "weighted_compound": round(summary.weighted_compound, 4),
        "total_engagement": summary.total_engagement,
        "top_positive": [p.to_record() for p in summary.top_positive[:examples]],
        "top_negative": [p.to_record() for p in summary.top_negative[:examples]],
    }


def _print_human(run: CompanyRun, examples: int, stream) -> None:
    summary: CompanySentiment = run.summary
    print(summary.summary(), file=stream)
    print(
        f"  fetched {run.fetched}, kept {run.kept} ({run.drop_report()})",
        file=stream,
    )
    for heading, posts in (
        ("most positive", summary.top_positive),
        ("most negative", summary.top_negative),
    ):
        for post in posts[:examples]:
            text = " ".join(post.post.text.split())[:110]
            print(
                f"  {heading:>13} {post.sentiment.compound:+.2f}  {text}",
                file=stream,
            )
    print(file=stream)


def main(argv=None, stream=None, transport=None) -> int:
    """Run a scrape. ``transport`` is an injection seam for end-to-end tests;
    left as None it means the real HTTP transport."""
    load_dotenv()
    stream = stream or sys.stdout
    args = build_parser().parse_args(argv)

    try:
        config = _resolve_config(args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    if args.dry_run:
        for company in config.companies:
            print(f"{company.name}: {company.build_query()}", file=stream)
        return EXIT_OK

    try:
        token = bearer_token()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    scorer = SentimentScorer(
        extra_lexicon=config.extra_lexicon, extra_phrases=config.extra_phrases
    )
    client = XSearchClient(token, transport=transport)
    store = ResultStore(config.output_path)
    state = RunState(config.state_path)
    if args.fresh:
        for company in config.companies:
            state.reset(company.name)

    runs = []
    try:
        for company in config.companies:
            run = run_company(client, scorer, company, state)
            store.append(p.to_record() for p in run.scored)
            runs.append(run)
    except XApiError as exc:
        state.save()
        print(f"X API error: {exc}", file=sys.stderr)
        return EXIT_API

    state.save()

    if args.json:
        print(
            json.dumps(
                {
                    "requests_made": client.requests_made,
                    "companies": [_run_payload(r, args.examples) for r in runs],
                },
                ensure_ascii=False,
                indent=2,
            ),
            file=stream,
        )
    else:
        for run in runs:
            _print_human(run, args.examples, stream)
        print(
            f"{client.requests_made} API request(s); "
            f"results appended to {config.output_path}",
            file=stream,
        )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
