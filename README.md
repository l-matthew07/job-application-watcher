# Google Careers Apply-Button Watcher

Watches one or more Google Careers job postings until their **Apply** button
goes live, then notifies you. Useful for roles that are listed before
applications formally open.

## How it works

Live Google Careers postings server-render the apply link
(`<a id="apply-action-button" aria-label="Apply" href="./apply?jobId=…">`)
directly into the page HTML, so a plain HTTP fetch detects it — no browser
automation needed. A posting that's up but not yet accepting applications
simply lacks that link.

This repo contains three implementations — pick the one that fits how you
want to run it:

| Implementation | Runs on | Alerts via |
|----------------|---------|------------|
| [`aws/`](#cloud-mode-aws-lambda--imessage) | AWS Lambda (free tier) | **iMessage** via Photon Spectrum — the current MVP |
| [`apply_watcher.py`](#local-mode-macos) | Your Mac (stdlib only) | macOS notification |
| [`apply_notifier.py`](#apply_notifierpy-slack--email--webhook) | Anywhere with Python | Slack / email / webhook |

## Local mode (macOS)

```bash
python3 apply_watcher.py "<job URL>" "<another job URL>" --interval 120
# or keep the list in a file (one URL per line):
python3 apply_watcher.py --urls-file urls.txt
```

When a button goes live it pops a notification with sound, prints the direct
apply link, and opens the page. Dead/mistyped URLs are flagged and dropped
(Google serves a generic "Jobs search" shell for those, recognized by the
page title). Run under `caffeinate -i … | tee watch.log` to keep the Mac
awake and keep a timestamped log.

Python 3 standard library only — nothing to install.

## Cloud mode (AWS Lambda + iMessage)

Runs every 5 minutes on AWS free tier and **iMessages everyone in
`RECIPIENTS`** the moment a button appears — one DM per event per
recipient, sent through [Photon Spectrum](https://photon.codes/docs/spectrum-ts)
Cloud, deduped via SSM Parameter Store state.

One-time setup:

```bash
aws configure                      # your AWS access key + region
cd aws
cp .env.example .env               # fill in Photon creds + JOB_URLS + RECIPIENTS
./deploy.sh                        # needs bun installed (builds the bundle)
```

`deploy.sh` is idempotent — it creates (or updates) the IAM role, the
`apply-watcher` Lambda (nodejs22.x, bundled with bun), and a
`rate(5 minutes)` EventBridge schedule, then invokes it once as a smoke
test. Re-run it after changing `.env` or the code.

Photon: `PROJECT_ID` / `PROJECT_SECRET` come from your project Settings
on the [dashboard](https://app.photon.codes). Every number in
`RECIPIENTS` must be registered there as a project user (shared-pool
plan requirement). **`.env` is gitignored — never commit credentials.**

Watch it run:

```bash
aws logs tail /aws/lambda/apply-watcher --follow
```

Cost: ~8,600 invocations/month — comfortably inside the Lambda and
EventBridge free tiers; Photon's free plan covers the messaging.

## `apply_notifier.py` (Slack / email / webhook)

A more configurable watcher (from
[bartut22/google-apply-notifier](https://github.com/bartut22/google-apply-notifier))
that classifies the apply element into one of three states:

| Status     | Meaning                                                              |
|------------|-----------------------------------------------------------------------|
| `open`     | A live `<a aria-label="Apply">` with a working `href` is present.     |
| `disabled` | The element exists in the DOM but has no usable `href` (not yet active). |
| `missing`  | No apply element found at all (page structure changed, job closed, or blocked). |

A notification fires only on a transition **into** `open`, and state is
persisted to disk (`state.json`) so restarts don't re-send duplicate alerts.

Features:

- Robust selector (`aria-label="Apply"` + `href` check) instead of brittle, frequently-rotated Google CSS class names.
- Exponential backoff retries on network errors.
- Randomized jitter on the poll interval to avoid a fixed, bot-like cadence.
- Pluggable notifications: console (always on), Slack webhook, generic webhook (Discord/ntfy/etc.), and email/SMTP.
- Rotating log file plus stdout logging.
- Graceful shutdown on `Ctrl+C` / `SIGTERM`.

Setup:

```bash
pip install -r requirements.txt
cp config.example.yaml config.yaml   # edit with your job URLs
cp .env.example .env                 # optional: notification credentials
```

Usage:

```bash
# Using a config file
python apply_notifier.py --config config.yaml

# Or pass one or more URLs directly on the CLI
python apply_notifier.py --url "https://www.google.com/about/careers/applications/jobs/results/..." --interval 300

# Run a single check and exit (good for cron / CI instead of a long-running loop)
python apply_notifier.py --config config.yaml --once
```

Enable notification channels via environment variables (or `.env`):

| Channel  | Required env vars |
|----------|--------------------|
| Slack    | `SLACK_WEBHOOK_URL` |
| Webhook (Discord/ntfy/custom) | `WEBHOOK_URL` |
| Email    | `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `NOTIFY_EMAIL_TO` |

## Serving many subscribers (`platform/`)

The modes above are single-user. [`platform/`](platform/README.md) is the
multi-subscriber architecture: a signup page → API Gateway → Lambda →
DynamoDB intake with email double opt-in, and a scheduled watcher that
fans out through SQS to SES email and Twilio SMS workers. Built to serve
250k+ subscribers; see its README for deploy steps and the SES/Twilio
compliance checklist that scale requires. **Currently parked as the
phase-2 path** — distribution happens via social for now, and this stack
gets picked back up when signup opens to the public.

## Company sentiment on X (`twitter_sentiment/`)

Unrelated to the apply-button watchers above: a scraper that pulls recent
public posts about a list of companies from X, filters out the noise, scores
what's left for sentiment, and rolls it up per company.

```bash
pip install -r requirements.txt
cp companies.example.yaml companies.yaml   # edit the company list
echo 'X_BEARER_TOKEN=...' >> .env

python -m twitter_sentiment --config companies.yaml --dry-run   # inspect queries
python -m twitter_sentiment --config companies.yaml             # run it
python -m twitter_sentiment --company Rivian --term rivian --term '$RIVN'
```

```
Tesla: 4 posts | +1 ~1 -2 | net -0.25 | mean -0.151 (engagement-weighted -0.108)
  fetched 9, kept 4 (promo_spam=1, no_term_match=1, unsupported_lang=1, too_short=1)
  most positive +0.84  Tesla's new FSD build is genuinely excellent, best update yet
  most negative -0.81  third Tesla service center outage this week, absolutely terrible
```

Add `--json` for a machine-readable version of the same thing.

### Why the API and not HTML scraping

This reads `GET /2/tweets/search/recent` with an app-only bearer token.
Scraping x.com unauthenticated violates X's terms of service, and in practice
means guest-token acquisition, aggressive IP blocking and markup that rotates
without notice. The `Transport` protocol in `client.py` is the seam if a
different backend is ever needed — the rest of the pipeline doesn't care.

Two consequences worth knowing before you plan around this:

- **Recent search only covers the last 7 days.** There is no backfill here; to
  build history you run it on a schedule and let `sentiment.jsonl` accumulate.
- **Recent search requires a paid access tier.** The free tier does not include
  it. Check current pricing at <https://docs.x.com/x-api> — it has changed
  repeatedly.

### How a post becomes a number

| Stage | What it does |
|-------|--------------|
| **Query** | `terms` OR'd together, `exclude_terms` negated, plus `-is:retweet` / `lang:` |
| **Relevance** | Terms re-checked locally with word boundaries — the API tokenizes loosely, so `Teslamania` comes back for a `tesla` query and has to be dropped here |
| **Quality** | Promo spam, hashtag/mention stuffing, one-word posts, and brand-new low-follower accounts |
| **Dedupe** | Copypasta collapsed by a text fingerprint that ignores links, handles and punctuation |
| **Score** | VADER plus a domain lexicon (see below) |
| **Rollup** | Counts, plain mean, engagement-weighted mean, strongest examples each way |

Every drop is counted by reason and printed. That's deliberate — the way a
scraper like this fails is silently: a query stops matching, volume goes to
zero, and the sentiment number looks calm rather than broken.

### Sentiment

[VADER](https://github.com/cjhutto/vaderSentiment) is the base — it was built
for social text, so emoji, ALL-CAPS, `!!!`, degree modifiers and negation all
work with no training step. What it doesn't have is the vocabulary people use
about *companies*: stock VADER scores "layoffs announced today" at a flat
`0.0`, along with `outage`, `bricked`, `buggy` and `overpriced`.
`sentiment.py` adds those, plus multi-word terms like `data breach`, which
VADER can't express at all since it scores whitespace tokens.

You can extend both from your config:

```yaml
extra_lexicon:                 # single words, VADER's -4.0..+4.0 scale
  vaporware: -2.5
extra_phrases:                 # multi-word terms
  supply chain delay: -2.0
```

Limits, in rough order of how much they'll bite:

- **Sarcasm is unsolved.** "great, another outage" scores positive. No lexicon
  method fixes this.
- **English only.** Other languages are skipped and counted as
  `unsupported_lang` rather than scored 0.0 — a flat zero is indistinguishable
  from real neutrality and would drag every rollup toward the middle.
- **Polarity is not aboutness.** A post can be angry at a replier while
  mentioning the company neutrally.

### Incremental runs

State lives in `sentiment_state.json`: a per-company `since_id` high-water
mark, plus a bounded ring of recent text fingerprints. `since_id` works
because X IDs are snowflakes, so the largest one seen means "everything before
this is handled". The fingerprint ring covers what `since_id` can't — the same
copypasta reposted under a new ID. Results append to `sentiment.jsonl`, one
scored post per line. `--fresh` ignores the high-water mark and re-scans.

### Tests

```bash
pip install -r requirements-dev.txt
python -m pytest tests/
```

No network: the HTTP transport is faked, everything below it is the real code
path. `tests/test_e2e.py` drives the actual CLI through config parsing,
filtering, scoring, JSONL output and resume-across-runs.

## Notes / limitations

- This only detects a DOM-level "Apply" link becoming active. If Google changes the markup or blocks automated requests (e.g., CAPTCHA), the watchers log a `missing` status rather than crash, but won't know a role opened until the selector is updated.
- Respect Google's Terms of Service and `robots.txt` when polling; keep intervals reasonable (default is 5 minutes) and avoid tight, aggressive loops.
- This is a monitoring tool only — it does not submit applications on your behalf.
