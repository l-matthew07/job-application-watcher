/**
 * Tests for the admin page's company detection.
 *
 * The bug these exist for: typing "doordash" found nothing, because DoorDash's
 * Greenhouse board is "doordashusa". The miss then took the failure path,
 * which called Photon — and with Photon down that blew API Gateway's hard 30s
 * integration timeout, so the page returned {"message":"Service Unavailable"}
 * instead of saying what was wrong.
 *
 * fetch is stubbed, so these run offline and assert on the requests made.
 */
import { describe, expect, mock, test, beforeEach, afterEach } from "bun:test";
import { detectCompany, slugCandidates, titleKey } from "./index";

const realFetch = globalThis.fetch;
let requested: string[] = [];

/** Serve a board only at the given URLs; 404 everything else. */
function serveBoards(boards: Record<string, unknown>) {
  globalThis.fetch = mock(async (input: RequestInfo | URL) => {
    const url = String(input);
    requested.push(url);
    const hit = Object.entries(boards).find(([frag]) => url.includes(frag));
    if (!hit) return new Response("not found", { status: 404 });
    return new Response(JSON.stringify(hit[1]), {
      status: 200, headers: { "Content-Type": "application/json" },
    });
  }) as unknown as typeof fetch;
}

const ghBoard = (n: number) => ({
  jobs: Array.from({ length: n }, (_, i) => ({
    id: 1000 + i, title: `Job ${i}`,
    absolute_url: `https://job-boards.greenhouse.io/x/jobs/${1000 + i}`,
    location: { name: "SF" },
  })),
});

beforeEach(() => { requested = []; });
afterEach(() => { globalThis.fetch = realFetch; });

describe("slugCandidates", () => {
  test("includes the regional suffix that DoorDash's board uses", () => {
    expect(slugCandidates("doordash")).toContain("doordashusa");
  });

  test("puts the literal slug first, so exact boards win", () => {
    expect(slugCandidates("Notion")[0]).toBe("notion");
  });

  test("strips punctuation and offers a hyphen-free variant", () => {
    const out = slugCandidates("Foo-Bar, Inc.");
    expect(out[0]).toBe("foo-bar");
    expect(out).toContain("foobar");
  });

  test("de-duplicates", () => {
    expect(new Set(slugCandidates("acme")).size).toBe(slugCandidates("acme").length);
  });

  test("empty input yields no candidates", () => {
    expect(slugCandidates("!!!")).toEqual([]);
  });
});

describe("detectCompany — the DoorDash case", () => {
  test("finds a board under a suffixed slug", async () => {
    serveBoards({ "/boards/doordashusa/jobs": ghBoard(42) });

    const res = await detectCompany("doordash");

    expect(res).not.toHaveProperty("error");
    if ("error" in res) throw new Error(res.error);
    expect(res.company.provider).toBe("greenhouse");
    expect(res.company.slug).toBe("doordashusa");
    expect(res.company.name).toBe("Doordash");
    expect(res.count).toBe(42);
  });

  test("an exact slug beats a bigger suffixed board", async () => {
    serveBoards({
      "/boards/acme/jobs": ghBoard(3),
      "/boards/acmeinc/jobs": ghBoard(900),
    });

    const res = await detectCompany("acme");

    if ("error" in res) throw new Error(res.error);
    expect(res.company.slug).toBe("acme");
  });

  test("a genuine miss reports what it tried, and does not throw", async () => {
    serveBoards({});

    const res = await detectCompany("notarealcompanyxyz");

    expect(res).toHaveProperty("error");
    if (!("error" in res)) throw new Error("expected an error");
    expect(res.error).toContain("notarealcompanyxyz");
    expect(res.error).toContain("also tried");
  });

  test("an empty board is not a hit", async () => {
    serveBoards({ "/boards/ghosttown/jobs": ghBoard(0) });

    expect(await detectCompany("ghosttown")).toHaveProperty("error");
  });
});

describe("detectCompany — URL input", () => {
  test("a pasted Greenhouse board URL skips the slug sweep entirely", async () => {
    serveBoards({ "/boards/doordashusa/jobs": ghBoard(42) });

    const res = await detectCompany("https://job-boards.greenhouse.io/doordashusa");

    if ("error" in res) throw new Error(res.error);
    expect(res.company.slug).toBe("doordashusa");
    // One request, not a dozen — this is the path that stays well inside
    // API Gateway's 30s cap.
    expect(requested.length).toBe(1);
  });

  test("a non-ATS URL is rejected without any network call", async () => {
    serveBoards({});

    const res = await detectCompany("https://careers.doordash.com/");

    expect(res).toHaveProperty("error");
    expect(requested.length).toBe(0);
  });

  test("blank input is rejected without any network call", async () => {
    serveBoards({});

    expect(await detectCompany("   ")).toHaveProperty("error");
    expect(requested.length).toBe(0);
  });
});

describe("detection request budget", () => {
  test("the whole sweep is issued concurrently", async () => {
    let inFlight = 0, peak = 0;
    globalThis.fetch = mock(async () => {
      peak = Math.max(peak, ++inFlight);
      await new Promise(r => setTimeout(r, 5));
      inFlight--;
      return new Response("not found", { status: 404 });
    }) as unknown as typeof fetch;

    await detectCompany("doordash");

    // Sequential probing would blow the 30s gateway cap; parallel is the
    // property that keeps a miss cheap.
    expect(peak).toBeGreaterThan(5);
  });
});

describe("titleKey", () => {
  test("normalizes whitespace and case so reposts don't re-alert", () => {
    expect(titleKey("Amazon", "  Software   Dev  Intern "))
      .toBe(titleKey("Amazon", "software dev intern"));
  });

  test("keeps companies separate", () => {
    expect(titleKey("Amazon", "SDE Intern")).not.toBe(titleKey("Notion", "SDE Intern"));
  });
});
