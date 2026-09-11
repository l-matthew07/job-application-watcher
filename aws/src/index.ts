/**
 * AWS Lambda: multi-company job-posting watcher with Photon Spectrum
 * iMessage alerts, plus a self-serve admin page for adding companies.
 *
 * Watcher (EventBridge schedule): reads every tracked company's job board —
 * Notion + Amazon are built in; more can be added at runtime — and alerts
 * (iMessage + optional SES email) when a posting whose title matches
 * TITLE_PATTERN goes live, with the direct application link. A title that
 * was live and later disappears from its company's board triggers a one-time
 * "closed" alert. Alerts dedupe on company + normalized title, so duplicate
 * ids and reposts stay silent. State lives in SSM.
 *
 * Admin page (Lambda Function URL, ?key=ADMIN_KEY): enter a company name or
 * careers-page URL; the handler auto-detects its ATS — Ashby, Greenhouse, or
 * Lever, which all expose public JSON boards — validates the board exists,
 * and saves it to the COMPANIES_PARAM SSM list that the watcher reads each
 * run. Companies can be removed the same way.
 *
 * Environment variables:
 *     TITLE_PATTERN   case-insensitive regex; any posting title matching it
 *                     is watched (catches postings that don't exist yet)
 *     ADMIN_KEY       secret for the admin page; unset disables the page
 *     JOB_URLS        optional Ashby posting URLs (Notion) to watch by id
 *     AMAZON_COUNTRIES  optional ISO3 codes for amazon.jobs (default USA,CAN)
 *     PROJECT_ID      Spectrum Cloud project (Photon dashboard → Settings)
 *     PROJECT_SECRET  its API secret
 *     RECIPIENTS      comma-separated E.164 numbers; each must be a
 *                     registered project user on the Photon dashboard
 *     EMAIL_FROM      optional; SES-verified sender for email alerts
 *     EMAIL_TO        optional; comma-separated email alert recipients
 *     STATE_PARAM     SSM parameter name (default /apply-watcher/state)
 *     COMPANIES_PARAM SSM parameter name (default /apply-watcher/companies)
 */

import { Spectrum } from "spectrum-ts";
import { imessage } from "@spectrum-ts/imessage";
import {
  SSMClient,
  GetParameterCommand,
  PutParameterCommand,
} from "@aws-sdk/client-ssm";
import { SESv2Client, SendEmailCommand } from "@aws-sdk/client-sesv2";

const AMAZON_API = "https://www.amazon.jobs/en/search.json";
const AMAZON_JOB = "https://www.amazon.jobs/en/jobs";
const UA =
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) " +
  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36";

const ssm = new SSMClient({});
const ses = new SESv2Client({});

type Status = "live" | "waiting" | "gone" | "error";
type Provider = "ashby" | "greenhouse" | "lever";
type Company = { name: string; provider: Provider; slug: string };
type Job = {
  id: string;
  title: string;
  location?: string;
  jobUrl: string;
  applyUrl: string;
  company: string;
};
type Board = { jobs: Map<string, Job>; ok: boolean };
// titles/origins remember pattern-discovered postings (url -> title/company)
// so we can name them in "closed" alerts after they've been delisted.
type State = {
  notified: string[];
  titles?: Record<string, string>;
  origins?: Record<string, string>;
};

const NOTION: Company = { name: "Notion", provider: "ashby", slug: "notion" };

function jobIdFromUrl(url: string): string | null {
  const m = url.match(/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/i);
  return m?.[1]?.toLowerCase() ?? null;
}

// Legacy fallback for state entries written before origins were tracked.
function companyFromUrl(url: string): string {
  return url.includes("amazon.jobs") ? "Amazon" : "Notion";
}

// Alert-dedupe key: companies (Amazon especially) post the same role under
// several job ids and repost under fresh ids later. One title = one alert.
export function titleKey(companyName: string, title: string): string {
  return `${companyName}:${title.toLowerCase().replace(/\s+/g, " ").trim()}`;
}

// The watcher runs on a 120s Lambda timeout and can afford to wait out a slow
// board. The admin page cannot: it sits behind API Gateway, whose integration
// timeout is a hard 30s maximum, and a request that exceeds it comes back as a
// bare {"message":"Service Unavailable"} with no detail. Detection therefore
// uses DETECT_TIMEOUT_MS, not this.
const FETCH_TIMEOUT_MS = 25_000;
const DETECT_TIMEOUT_MS = 6_000;

async function getBody(
  url: string,
  accept?: string,
  timeoutMs: number = FETCH_TIMEOUT_MS,
): Promise<string | null> {
  try {
    const resp = await fetch(url, {
      headers: { "User-Agent": UA, ...(accept ? { Accept: accept } : {}) },
      signal: AbortSignal.timeout(timeoutMs),
    });
    if (!resp.ok) return null;
    return await resp.text();
  } catch {
    return null;
  }
}

async function getJson(
  url: string,
  timeoutMs: number = FETCH_TIMEOUT_MS,
): Promise<unknown | null> {
  const body = await getBody(url, "application/json", timeoutMs);
  if (body === null) return null;
  try {
    return JSON.parse(body);
  } catch {
    return null;
  }
}

// --- Providers ---------------------------------------------------------------

async function fetchAshbyApi(c: Company, timeoutMs?: number): Promise<Map<string, Job> | null> {
  const data = (await getJson(
    `https://api.ashbyhq.com/posting-api/job-board/${encodeURIComponent(c.slug)}`,
    timeoutMs,
  )) as { jobs?: { id: string; title: string; location?: string; jobUrl: string; applyUrl: string }[] } | null;
  if (!data) return null;
  return new Map((data.jobs ?? []).map(j => [j.id.toLowerCase(), {
    id: j.id.toLowerCase(),
    title: j.title,
    location: j.location,
    jobUrl: `https://jobs.ashbyhq.com/${c.slug}/${j.id.toLowerCase()}`,
    applyUrl: j.applyUrl ?? `https://jobs.ashbyhq.com/${c.slug}/${j.id.toLowerCase()}/application`,
    company: c.name,
  }]));
}

// Redundant second Ashby source: the board page server-renders the job list
// as JSON in the HTML. If the posting API's cache ever lags, this is where a
// new posting shows up first (it's what a human visiting the page sees).
async function fetchAshbyPage(c: Company, timeoutMs?: number): Promise<Map<string, Job> | null> {
  const html = await getBody(
    `https://jobs.ashbyhq.com/${encodeURIComponent(c.slug)}`, undefined, timeoutMs);
  if (html === null) return null;
  const re = /\{"id":"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})","title":"((?:[^"\\]|\\.)*)"(?:[^{}]*?"locationName":"((?:[^"\\]|\\.)*)")?/gi;
  const jobs = new Map<string, Job>();
  for (const m of html.matchAll(re)) {
    const id = m[1]!.toLowerCase();
    jobs.set(id, {
      id,
      title: JSON.parse(`"${m[2]}"`),
      location: m[3] ? JSON.parse(`"${m[3]}"`) : undefined,
      jobUrl: `https://jobs.ashbyhq.com/${c.slug}/${id}`,
      applyUrl: `https://jobs.ashbyhq.com/${c.slug}/${id}/application`,
      company: c.name,
    });
  }
  // Zero matches means the markup changed — a source failure, not an empty
  // board.
  return jobs.size > 0 ? jobs : null;
}

async function fetchGreenhouse(c: Company, timeoutMs?: number): Promise<Map<string, Job> | null> {
  const data = (await getJson(
    `https://boards-api.greenhouse.io/v1/boards/${encodeURIComponent(c.slug)}/jobs`,
    timeoutMs,
  )) as { jobs?: { id: number; title: string; absolute_url: string; location?: { name?: string } }[] } | null;
  if (!data) return null;
  return new Map((data.jobs ?? []).map(j => [`gh-${c.slug}-${j.id}`, {
    id: `gh-${c.slug}-${j.id}`,
    title: j.title,
    location: j.location?.name,
    jobUrl: j.absolute_url,
    applyUrl: j.absolute_url,
    company: c.name,
  }]));
}

async function fetchLever(c: Company, timeoutMs?: number): Promise<Map<string, Job> | null> {
  const data = (await getJson(
    `https://api.lever.co/v0/postings/${encodeURIComponent(c.slug)}?mode=json`,
    timeoutMs,
  )) as { id: string; text: string; hostedUrl: string; applyUrl?: string; categories?: { location?: string } }[] | null;
  if (!data || !Array.isArray(data)) return null;
  return new Map(data.map(j => [`lv-${c.slug}-${j.id}`, {
    id: `lv-${c.slug}-${j.id}`,
    title: j.text,
    location: j.categories?.location,
    jobUrl: j.hostedUrl,
    applyUrl: j.applyUrl ?? j.hostedUrl,
    company: c.name,
  }]));
}

async function fetchCompanyBoard(c: Company, timeoutMs?: number): Promise<Board | null> {
  if (c.provider === "ashby") {
    const [api, page] = await Promise.all([
      fetchAshbyApi(c, timeoutMs), fetchAshbyPage(c, timeoutMs),
    ]);
    if (api === null && page === null) return null;
    // Union of both sources; API entries win (richer location/applyUrl).
    // "ok" (trust close detection) requires both to have answered.
    return {
      jobs: new Map([...(page ?? []), ...(api ?? [])]),
      ok: api !== null && page !== null,
    };
  }
  const jobs = c.provider === "greenhouse"
    ? await fetchGreenhouse(c, timeoutMs)
    : await fetchLever(c, timeoutMs);
  return jobs === null ? null : { jobs, ok: true };
}

// Amazon (amazon.jobs) is custom — no public ATS. Two light queries per run
// instead of paging all ~1.8k software-development postings: the 100 most
// recent (new postings always enter at the top of sort=recent, so discovery
// can't miss them), plus a full-text "intern" search that keeps tracking
// older intern postings so close detection still works after they age out
// of the recent window.
async function fetchAmazon(): Promise<Board | null> {
  const countries = (process.env.AMAZON_COUNTRIES ?? "USA,CAN")
    .split(",").map(s => s.trim()).filter(Boolean);
  const base =
    `${AMAZON_API}?category%5B%5D=software-development&sort=recent` +
    `&result_limit=100&offset=0` +
    countries.map(c => `&normalized_country_code%5B%5D=${c}`).join("");
  const maps = await Promise.all([base, `${base}&base_query=intern`].map(async q => {
    const data = (await getJson(q)) as {
      jobs?: { id_icims: string; title: string; normalized_location?: string; job_path: string }[];
    } | null;
    if (!data) return null;
    return new Map<string, Job>((data.jobs ?? []).map(j => [`amzn-${j.id_icims}`, {
      id: `amzn-${j.id_icims}`,
      title: j.title,
      location: j.normalized_location,
      jobUrl: `${AMAZON_JOB}/${j.id_icims}`,
      applyUrl: `https://www.amazon.jobs${j.job_path}`,
      company: "Amazon",
    }]));
  }));
  const okMaps = maps.filter((m): m is Map<string, Job> => m !== null);
  if (okMaps.length === 0) return null;
  return { jobs: new Map(okMaps.flatMap(m => [...m])), ok: okMaps.length === maps.length };
}

// --- SSM state ---------------------------------------------------------------

async function loadParam(param: string): Promise<string | null> {
  try {
    const out = await ssm.send(new GetParameterCommand({ Name: param }));
    return out.Parameter?.Value ?? null;
  } catch {
    return null;
  }
}

async function saveParam(param: string, value: string): Promise<void> {
  await ssm.send(new PutParameterCommand({
    Name: param, Value: value, Type: "String", Overwrite: true,
  }));
}

async function loadState(param: string): Promise<State> {
  try {
    return JSON.parse((await loadParam(param)) ?? "");
  } catch {
    return { notified: [] };
  }
}

function companiesParam(): string {
  return process.env.COMPANIES_PARAM ?? "/apply-watcher/companies";
}

async function loadCompanies(): Promise<Company[]> {
  try {
    const list = JSON.parse((await loadParam(companiesParam())) ?? "[]");
    return Array.isArray(list) ? list : [];
  } catch {
    return [];
  }
}

// --- Alerts ------------------------------------------------------------------

async function emailAlerts(alerts: string[]): Promise<void> {
  const from = process.env.EMAIL_FROM;
  const to = (process.env.EMAIL_TO ?? "")
    .split(",").map(s => s.trim()).filter(Boolean);
  if (!from || to.length === 0) return;
  try {
    await ses.send(new SendEmailCommand({
      FromEmailAddress: from,
      Destination: { ToAddresses: to },
      Content: {
        Simple: {
          Subject: { Data: "Apply-watcher alert" },
          Body: { Text: { Data: alerts.join("\n\n") } },
        },
      },
    }));
    console.log(`email -> ${to.join(", ")}: sent`);
  } catch (e) {
    // Likely SES sandbox (recipient unverified) or identity not yet
    // verified. Don't block the iMessage path.
    console.error(`email -> ${to.join(", ")}: FAILED — ${e}`);
  }
}

async function sendText(recipients: string[], text: string): Promise<void> {
  const app = await Spectrum({
    projectId: process.env.PROJECT_ID!,
    projectSecret: process.env.PROJECT_SECRET!,
    providers: [imessage.config()],
  });
  try {
    const im = imessage(app);
    for (const phone of recipients) {
      try {
        const dm = await im.space.create(await im.user(phone));
        await dm.send(text);
        console.log(`iMessage -> ${phone}: sent`);
      } catch (e) {
        // Likely "Target not allowed": number not registered as a project
        // user on the Photon dashboard. Don't block the other sends.
        console.error(`iMessage -> ${phone}: FAILED — ${e}`);
      }
    }
  } finally {
    await app.stop();
  }
}

async function sendAlerts(alerts: string[]): Promise<void> {
  const recipients = (process.env.RECIPIENTS ?? "")
    .split(",").map(s => s.trim()).filter(Boolean);
  // One combined message per recipient — a burst of postings (e.g. first
  // scan of a newly added company) shouldn't be a burst of texts.
  await sendText(recipients, alerts.join("\n\n"));
}

// When the admin page can't auto-detect a company, the maintainer (first
// RECIPIENTS number) gets told so the provider can be built out manually.
//
// The note is QUEUED, not sent. The admin page runs behind API Gateway's hard
// 30s integration timeout, and booting Spectrum to send an iMessage is slow —
// and when Photon is down it can hang straight past that cap, turning a
// perfectly ordinary "couldn't find that company" into an opaque
// {"message":"Service Unavailable"}. The scheduled watch run has a 120s budget
// and no gateway in front of it, so it does the sending. The queue also means
// the note survives Photon being broken, which is exactly when it is most
// likely to be needed.
function pendingParam(): string {
  return process.env.PENDING_PARAM ?? "/apply-watcher/pending-admin";
}

const MAX_PENDING_NOTES = 20;

async function loadPendingNotes(): Promise<string[]> {
  try {
    const list = JSON.parse((await loadParam(pendingParam())) ?? "[]");
    return Array.isArray(list) ? list.map(String) : [];
  } catch {
    return [];
  }
}

async function queueAdminNote(text: string): Promise<void> {
  try {
    const pending = await loadPendingNotes();
    pending.push(text);
    await saveParam(
      pendingParam(), JSON.stringify(pending.slice(-MAX_PENDING_NOTES)));
  } catch (e) {
    // A queue failure must not fail the admin request — the page still shows
    // the reason on screen, which is the part the user is waiting for.
    console.error(`queue admin note FAILED — ${e}`);
  }
}

async function flushAdminNotes(): Promise<void> {
  const pending = await loadPendingNotes();
  if (pending.length === 0) return;
  const admin = (process.env.RECIPIENTS ?? "")
    .split(",").map(s => s.trim()).filter(Boolean)[0];
  if (!admin) return;
  // Only clear once the send actually returned. If Spectrum can't start, the
  // notes stay queued for the next run rather than vanishing.
  await sendText([admin], pending.join("\n\n"));
  await saveParam(pendingParam(), "[]");
  console.log(`flushed ${pending.length} queued admin note(s)`);
}

// --- Watcher -----------------------------------------------------------------

async function runWatch(): Promise<Record<string, Status>> {
  const param = process.env.STATE_PARAM ?? "/apply-watcher/state";
  const urls = (process.env.JOB_URLS ?? "")
    .split(/[,\n]/).map(s => s.trim()).filter(Boolean);
  const state = await loadState(param);
  const results: Record<string, Status> = {};
  const alerts: string[] = [];

  const extra = await loadCompanies();
  const companies = [NOTION, ...extra.filter(c => c.name !== NOTION.name)];
  const boards = await Promise.all([
    ...companies.map(fetchCompanyBoard),
    fetchAmazon(),
  ]);
  const names = [...companies.map(c => c.name), "Amazon"];
  const okByCompany = new Map<string, boolean>();
  const combined = new Map<string, Job>();
  const counts: string[] = [];
  boards.forEach((b, i) => {
    const name = names[i]!;
    okByCompany.set(name, b?.ok ?? false);
    for (const [id, job] of b?.jobs ?? []) combined.set(id, job);
    counts.push(`${name}=${b ? `${b.jobs.size}${b.ok ? "" : " (partial)"}` : "FAIL"}`);
  });
  console.log(`boards: ${counts.join(" ")}`);

  // Explicitly watched Notion posting ids (legacy JOB_URLS feature).
  const notionBoard = boards[0];
  for (const url of urls) {
    const id = jobIdFromUrl(url);
    if (!id || !notionBoard) {
      results[url.slice(0, 80)] = "error";
      continue;
    }
    const job = notionBoard.jobs.get(id);
    const keyLive = `live:${url}`;
    const keyGone = `gone:${url}`;
    if (job) {
      results[job.title] = "live";
      if (!state.notified.includes(keyLive)) {
        alerts.push(
          `🚨 Notion posting is LIVE: ${job.title}` +
          (job.location ? ` (${job.location})` : "") +
          `\nApply now: ${job.applyUrl}`,
        );
        state.notified.push(keyLive);
      }
      state.notified = state.notified.filter(k => k !== keyGone);
    } else if (state.notified.includes(keyLive) && notionBoard.ok) {
      results[id] = "gone";
      if (!state.notified.includes(keyGone)) {
        alerts.push(`Notion posting closed (unlisted): ${url}`);
        state.notified.push(keyGone);
      }
      state.notified = state.notified.filter(k => k !== keyLive);
    } else {
      results[id] = "waiting";
    }
  }

  // Title-pattern watch across every company: catches postings that don't
  // exist yet.
  const pattern = process.env.TITLE_PATTERN
    ? new RegExp(process.env.TITLE_PATTERN, "i")
    : null;
  if (pattern && combined.size > 0) {
    state.titles ??= {};
    state.origins ??= {};
    for (const job of combined.values()) {
      if (!pattern.test(job.title)) continue;
      const norm = titleKey(job.company, job.title);
      const keyLive = `live:${norm}`;
      const keyGone = `gone:${norm}`;
      results[job.title] = "live";
      state.titles[job.jobUrl] = job.title;
      state.origins[job.jobUrl] = job.company;
      // Migrate pre-dedupe per-URL marks so those postings don't re-alert.
      if (state.notified.includes(`live:${job.jobUrl}`)) {
        state.notified = state.notified.filter(k => k !== `live:${job.jobUrl}`);
        if (!state.notified.includes(keyLive)) state.notified.push(keyLive);
      }
      if (!state.notified.includes(keyLive)) {
        alerts.push(
          `🚨 ${job.company} posting is LIVE: ${job.title}` +
          (job.location ? ` (${job.location})` : "") +
          `\nApply now: ${job.applyUrl}`,
        );
        state.notified.push(keyLive);
      }
      state.notified = state.notified.filter(
        k => k !== keyGone && k !== `gone:${job.jobUrl}`,
      );
    }
    // Pattern-discovered postings that have since been delisted. Only
    // trusted when that posting's own company board fully answered this run.
    const liveUrls = new Set([...combined.values()].map(j => j.jobUrl));
    const liveTitles = new Set(
      [...combined.values()]
        .filter(j => pattern.test(j.title))
        .map(j => titleKey(j.company, j.title)),
    );
    for (const [url, title] of Object.entries(state.titles)) {
      const origin = state.origins[url] ?? companyFromUrl(url);
      const prune = () => {
        delete state.titles![url];
        delete state.origins![url];
        state.notified = state.notified.filter(
          k => k !== `live:${url}` && k !== `gone:${url}`,
        );
      };
      // Pattern may have narrowed, or the company may have been removed
      // from tracking: stop following rather than alert on its close.
      if (!pattern.test(title) || !okByCompany.has(origin)) {
        prune();
        continue;
      }
      if (liveUrls.has(url) || urls.includes(url)) continue;
      if (okByCompany.get(origin) !== true) continue;
      // This id is gone either way; only alert if no other live posting
      // still carries the same title (repost/duplicate ids).
      prune();
      const norm = titleKey(origin, title);
      if (liveTitles.has(norm)) continue;
      results[title] = "gone";
      const keyLive = `live:${norm}`;
      const keyGone = `gone:${norm}`;
      if (state.notified.includes(keyLive) && !state.notified.includes(keyGone)) {
        alerts.push(`${origin} posting closed (unlisted): ${title}\n${url}`);
        state.notified.push(keyGone);
        state.notified = state.notified.filter(k => k !== keyLive);
      }
    }
    console.log(`pattern /${process.env.TITLE_PATTERN}/i matched ` +
      `${Object.values(results).filter(s => s === "live").length} live posting(s) ` +
      `of ${combined.size} across ${names.length} companies`);
  }

  if (alerts.length > 0) {
    await emailAlerts(alerts);
    await sendAlerts(alerts);
  }
  try {
    await flushAdminNotes();
  } catch (e) {
    // Photon being down must not fail the watch run — the notes stay queued.
    console.error(`flush admin notes FAILED — ${e}`);
  }
  await saveParam(param, JSON.stringify(state));
  console.log(JSON.stringify(results));
  return results;
}

// --- Admin page (Lambda Function URL) ----------------------------------------

function esc(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

async function probe(c: Company): Promise<number | null> {
  const board = await fetchCompanyBoard(c, DETECT_TIMEOUT_MS);
  return board === null ? null : board.jobs.size;
}

// A company name is rarely its board slug. DoorDash's Greenhouse board is
// "doordashusa", not "doordash" — and a user typing a company name has no
// reason to know that. Probing a few common variants turns a dead end into a
// hit. Exact matches still win over variants when both respond, so a real
// "acme" board is never shadowed by someone else's "acmeinc".
const CORPORATE_SUFFIXES = new Set(["inc", "llc", "corp", "co", "ltd", "plc", "gmbh"]);

export function slugCandidates(input: string): string[] {
  // Separators become hyphens rather than being deleted: dropping them turns
  // "Foo-Bar, Inc." into "foo-barinc", gluing the suffix onto the name.
  const words = input
    .toLowerCase()
    .split(/[^a-z0-9]+/)
    .filter(Boolean);
  // A trailing "Inc"/"Ltd" is almost never part of the board slug.
  while (words.length > 1 && CORPORATE_SUFFIXES.has(words[words.length - 1]!)) {
    words.pop();
  }
  const base = words.join("-");
  const compact = words.join("");
  // No alphanumerics at all means there is nothing to guess from. Returning
  // the bare suffixes here would probe boards literally named "usa" or "inc"
  // and could match an unrelated company.
  if (!compact) return [];
  const variants = [base, compact, `${compact}usa`, `${compact}inc`, `${compact}careers`];
  return [...new Set(variants.filter(Boolean))];
}

// Figure out which ATS a company input refers to. Accepts a board/careers
// URL (ashbyhq.com/x, greenhouse.io/x, lever.co/x) or a bare company name,
// which is probed against all three providers.
export async function detectCompany(
  input: string,
): Promise<{ company: Company; count: number } | { error: string }> {
  const s = input.trim();
  if (!s) return { error: "Enter a company name or careers URL." };
  const fromUrl: [RegExp, Provider][] = [
    [/ashbyhq\.com\/([A-Za-z0-9._%-]+)/i, "ashby"],
    [/greenhouse\.io\/(?:v1\/boards\/|embed\/job_board\?for=)?([A-Za-z0-9._%-]+)/i, "greenhouse"],
    [/lever\.co\/(?:v0\/postings\/)?([A-Za-z0-9._%-]+)/i, "lever"],
  ];
  for (const [re, provider] of fromUrl) {
    const m = s.match(re);
    if (!m) continue;
    const slug = decodeURIComponent(m[1]!).toLowerCase();
    const name = slug.charAt(0).toUpperCase() + slug.slice(1);
    const count = await probe({ name, provider, slug });
    if (count === null) return { error: `Found a ${provider} URL but its board "${slug}" doesn't respond.` };
    return { company: { name, provider, slug }, count };
  }
  if (/[\/:]/.test(s)) {
    return { error: "That URL isn't an Ashby, Greenhouse, or Lever board. Find the company's actual job board link (often behind the Apply button) and paste that." };
  }
  const name = s.charAt(0).toUpperCase() + s.slice(1);
  const providers: Provider[] = ["ashby", "greenhouse", "lever"];
  const candidates = slugCandidates(s);
  if (candidates.length === 0) {
    return { error: "Enter a company name or careers URL." };
  }
  // Every provider x slug combination at once. These are independent HTTP
  // reads on a short timeout, so the whole sweep costs one round trip, not N.
  const attempts = candidates.flatMap(slug =>
    providers.map(provider => ({ slug, provider })),
  );
  const counts = await Promise.all(
    attempts.map(a => probe({ name, provider: a.provider, slug: a.slug })),
  );
  const hits = attempts
    .map((a, i) => ({ ...a, count: counts[i], exact: a.slug === candidates[0] }))
    .filter((h): h is typeof h & { count: number } => h.count !== null && h.count > 0)
    // Exact slug first, then the fullest board.
    .sort((a, b) => Number(b.exact) - Number(a.exact) || b.count - a.count);
  if (hits.length === 0) {
    return { error: `No Ashby, Greenhouse, or Lever board found for "${candidates[0]}" (also tried ${candidates.slice(1).join(", ")}). Try pasting the company's job-board URL instead.` };
  }
  const best = hits[0]!;
  return {
    company: { name, provider: best.provider, slug: best.slug },
    count: best.count,
  };
}

function adminPage(companies: Company[], message: string): string {
  const rows = companies.map(c => `
    <li>
      <b>${esc(c.name)}</b> <small>(${esc(c.provider)}: ${esc(c.slug)})</small>
      <form method="post" style="display:inline">
        <input type="hidden" name="action" value="remove">
        <input type="hidden" name="name" value="${esc(c.name)}">
        <button type="submit">remove</button>
      </form>
    </li>`).join("");
  return `<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Apply-watcher</title>
<style>body{font-family:-apple-system,sans-serif;max-width:34rem;margin:2rem auto;padding:0 1rem;line-height:1.5}
input[type=text]{width:100%;padding:.5rem;font-size:1rem;box-sizing:border-box}
button{padding:.3rem .8rem;margin-left:.3rem}.msg{padding:.6rem;background:#eef;border-radius:6px}
li{margin:.4rem 0}</style></head><body>
<h2>📡 Apply-watcher</h2>
${message ? `<p class="msg">${message}</p>` : ""}
<p>Checks every 5 minutes for new engineering intern postings (summer-focused)
and texts the crew when one drops.</p>
<h3>Tracked companies</h3>
<ul>
  <li><b>Notion</b> <small>(built-in)</small></li>
  <li><b>Amazon</b> <small>(built-in, US+CAN)</small></li>
  ${rows}
</ul>
<h3>Add a company</h3>
<form method="post">
  <input type="hidden" name="action" value="add">
  <input type="text" name="company" placeholder="Company name (e.g. Stripe) or job-board URL" autofocus>
  <p><button type="submit">Add</button></p>
</form>
<p><small>Works with any company on Ashby, Greenhouse, or Lever (most tech
companies). Paste the careers/job-board URL if the name alone isn't found.</small></p>
</body></html>`;
}

type HttpEvent = {
  requestContext?: { http?: { method?: string } };
  queryStringParameters?: Record<string, string>;
  body?: string;
  isBase64Encoded?: boolean;
};

async function handleHttp(event: HttpEvent): Promise<object> {
  const html = (status: number, body: string) => ({
    statusCode: status,
    headers: { "Content-Type": "text/html; charset=utf-8" },
    body,
  });
  const key = process.env.ADMIN_KEY;
  if (!key || event.queryStringParameters?.key !== key) {
    return html(403, "<h3>403 — bad or missing key</h3>");
  }

  let message = "";
  if (event.requestContext?.http?.method === "POST") {
    const raw = event.isBase64Encoded
      ? Buffer.from(event.body ?? "", "base64").toString()
      : event.body ?? "";
    const form = new URLSearchParams(raw);
    const companies = await loadCompanies();
    if (form.get("action") === "add") {
      const res = await detectCompany(form.get("company") ?? "");
      if ("error" in res) {
        await queueAdminNote(
          `🛠️ Apply-watcher: a company couldn't be added via the admin page.\n` +
          `Input: "${(form.get("company") ?? "").trim().slice(0, 200)}"\n` +
          `Reason: ${res.error}\n` +
          `Needs a manual provider build-out.`,
        );
        message = `⚠️ ${esc(res.error)}<br>Matthew will be texted on the next ` +
          `run (within 5 minutes) and will wire this one up manually.`;
      } else if (
        [NOTION.name, "Amazon", ...companies.map(c => c.name)]
          .some(n => n.toLowerCase() === res.company.name.toLowerCase())
      ) {
        message = `${esc(res.company.name)} is already tracked.`;
      } else {
        companies.push(res.company);
        await saveParam(companiesParam(), JSON.stringify(companies));
        message = `✅ Added ${esc(res.company.name)} — ${res.company.provider} ` +
          `board "${esc(res.company.slug)}", ${res.count} postings live. The ` +
          `watcher picks it up within 5 minutes and texts if anything matches.`;
        // Success confirmation goes to the alert email (Sudo). Failures
        // deliberately do NOT go to him — only the maintainer is texted.
        await emailAlerts([
          `✅ Apply-watcher is now tracking ${res.company.name} ` +
          `(${res.company.provider}, ${res.count} postings live right now). ` +
          `You'll be alerted when a matching intern posting appears.`,
        ]);
      }
    } else if (form.get("action") === "remove") {
      const name = form.get("name") ?? "";
      const next = companies.filter(c => c.name !== name);
      await saveParam(companiesParam(), JSON.stringify(next));
      message = `Removed ${esc(name)}.`;
      return html(200, adminPage(next, message));
    }
  }
  return html(200, adminPage(await loadCompanies(), message));
}

// --- Entry -------------------------------------------------------------------

export const handler = async (event?: HttpEvent): Promise<unknown> => {
  if (event?.requestContext?.http) return handleHttp(event);
  return runWatch();
};
