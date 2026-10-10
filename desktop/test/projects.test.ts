import { describe, expect, it, vi } from "vitest";

import { FIXTURE_IDS, projectFixtures } from "../../web/src/lib/projects.js";
import { ANSWER_TIMEOUT_MS, LONG_ANSWER_TIMEOUT_MS, PageProjects, TimedOut, type ToPage } from "../src/shell/projects.js";

const { projects, threads, history, deleted } = projectFixtures(Date.parse("2026-10-06T12:00:00Z"));
const REPORT = FIXTURE_IDS.report;
const REVENUE = "threads/revenue/revenue.xlsx";

// The page, as the proxy meets it: what it was sent, and how to answer.
function page(timeoutMs?: number) {
  const sent: ToPage[] = [];
  const source = new PageProjects((message) => sent.push(message), timeoutMs);
  const last = () => sent.at(-1)!;
  return { sent, source, last };
}

describe("the projects the page serves", () => {
  it("are asked for through the page, and its answer is checked and copied field by field", async () => {
    const { source, last } = page();
    const listed = source.list();
    expect(last()).toEqual({ type: "call", id: 1, method: "list", args: [], deadline: expect.any(Number) });
    source.answered(1, { ok: projects.map((project) => ({ ...project, secret: "x" })) });
    const summaries = await listed;
    expect(summaries[0]).toEqual({
      id: REPORT, name: "Quarterly report", icon: null, createdAt: projects[0]!.createdAt, updatedAt: projects[0]!.updatedAt,
      waiting: 3, working: 2,
    });
    const rows = source.threads(REPORT);
    expect(last()).toEqual({ type: "call", id: 2, method: "threads", args: [REPORT], deadline: expect.any(Number) });
    source.answered(2, { ok: threads[REPORT] });
    expect(await rows).toEqual(threads[REPORT]);
  });

  it("take a thread that waits on you over its files", async () => {
    const { source } = page();
    const rows = source.threads(REPORT);
    const waiting = { ...threads[REPORT]![0]!, reason: "files", statusLine: "Couldn't merge my changes to Report.docx" };
    source.answered(1, { ok: [waiting] });
    expect(await rows).toEqual([waiting]);
  });

  it("refuse an answer that is not of its shape, and say the page's own error", async () => {
    const { source } = page();
    const wrong = source.threads(REPORT);
    source.answered(1, { ok: [{ ...threads[REPORT]![0], group: "landing" }] });
    await expect(wrong).rejects.toThrow("The agent's page answered threads with something Surogate cannot use");
    const refused = source.get("nope");
    source.answered(2, { error: "No such project" });
    await expect(refused).rejects.toThrow("No such project");
  });

  it("refuse a time with no zone, which would be read as local time, and a negative size", async () => {
    const { source } = page();
    const naive = source.list();
    source.answered(1, { ok: [{ ...projects[0], updatedAt: "2026-10-06T11:43:00" }] });
    await expect(naive).rejects.toThrow("The agent's page answered list with something Surogate cannot use");
    const negative = source.library(REPORT);
    source.answered(2, { ok: [{ path: "brief.docx", origin: "added", threadId: null, size: -1, updatedAt: null, place: { kind: "cloud" } }] });
    await expect(negative).rejects.toThrow("The agent's page answered library with something Surogate cannot use");
  });

  it("refuse a time that ends in Z but is no ISO 8601 UTC time, and take one with its fraction", async () => {
    const { source } = page();
    let id = 0;
    for (const updatedAt of ["1Z", "2026Z", "2026-10-06 11:43:00Z", "Oct 6 2026 11:43 +0200 Z", "2026-13-06T11:43:00Z"]) {
      const listed = source.list();
      source.answered(++id, { ok: [{ ...projects[0], updatedAt }] });
      await expect(listed, updatedAt).rejects.toThrow("The agent's page answered list with something Surogate cannot use");
    }
    const listed = source.list();
    source.answered(++id, { ok: [{ ...projects[0], updatedAt: "2026-10-06T11:43:00.123456Z" }] });
    expect((await listed)[0]!.updatedAt).toBe("2026-10-06T11:43:00.123456Z");
  });

  it("ask the page for one thread's row, or for every row", () => {
    const { source, last } = page();
    void source.threads(REPORT, FIXTURE_IDS.idle);
    expect(last()).toEqual({ type: "call", id: 1, method: "threads", args: [REPORT, FIXTURE_IDS.idle], deadline: expect.any(Number) });
    void source.threads(REPORT);
    expect(last()).toEqual({ type: "call", id: 2, method: "threads", args: [REPORT], deadline: expect.any(Number) });
  });

  it("hold a thread's one-row read to that row alone, or none, and a resolved or reopened row to the thread asked", async () => {
    const { source } = page();
    const question = threads[REPORT]!.find((row) => row.id === FIXTURE_IDS.question)!;
    const other = threads[REPORT]!.find((row) => row.id !== FIXTURE_IDS.question)!;
    const refused = "The agent's page answered %s with something Surogate cannot use";
    const another = source.threads(REPORT, question.id);
    source.answered(1, { ok: [other] });
    await expect(another).rejects.toThrow(refused.replace("%s", "threads"));
    const two = source.threads(REPORT, question.id);
    source.answered(2, { ok: [question, question] });
    await expect(two).rejects.toThrow(refused.replace("%s", "threads"));
    const none = source.threads(REPORT, question.id);
    source.answered(3, { ok: [] });
    expect(await none).toEqual([]);
    const alone = source.threads(REPORT, question.id);
    source.answered(4, { ok: [question] });
    expect(await alone).toEqual([question]);
    const resolved = source.resolve(REPORT, question.id);
    source.answered(5, { ok: other });
    await expect(resolved).rejects.toThrow(refused.replace("%s", "resolve"));
    const reopened = source.reopen(REPORT, question.id);
    source.answered(6, { ok: other });
    await expect(reopened).rejects.toThrow(refused.replace("%s", "reopen"));
  });

  it("send each call with the moment its time runs out, for a page that holds it", () => {
    vi.useFakeTimers({ now: 1_000 });
    try {
      const { source, last } = page(500);
      void source.list();
      expect(last()).toMatchObject({ type: "call", id: 1, deadline: 1_500 });
    } finally {
      vi.useRealTimers();
    }
  });

  it("refuse a call the page does not answer in time, and ignore an answer to no call", async () => {
    vi.useFakeTimers();
    try {
      const { source } = page(1_000);
      const listed = source.list();
      vi.advanceTimersByTime(1_000);
      await expect(listed).rejects.toThrow("The agent's page did not answer list in time");
      source.answered(1, { ok: [] });
      source.answered(99, { ok: [] });
    } finally {
      vi.useRealTimers();
    }
  });

  it("tell a subscriber what changed, until it unsubscribes", () => {
    const { source, last } = page();
    const heard: Array<string | null> = [];
    const unsubscribe = source.subscribe(REPORT, (threadId) => heard.push(threadId));
    expect(last()).toEqual({ type: "subscribe", id: 1, projectId: REPORT });
    source.changed(1, FIXTURE_IDS.idle);
    source.changed(1, null);
    source.changed(1, 42);
    unsubscribe();
    expect(last()).toEqual({ type: "unsubscribe", id: 1 });
    source.changed(1, null);
    expect(heard).toEqual([FIXTURE_IDS.idle, null]);
  });

  it("fail what waits, and end every subscription, once the page withdraws its source", async () => {
    const { source, sent } = page();
    const heard: Array<string | null> = [];
    source.subscribe(REPORT, (threadId) => heard.push(threadId));
    const listed = source.list();
    source.withdrawn();
    await expect(listed).rejects.toThrow("The agent's page no longer serves its projects");
    source.changed(1, null);
    expect(heard).toEqual([]);
    expect(sent).toHaveLength(2);
  });

  it("take a row's files with their marks, and a mark it does not know as no mark", async () => {
    const { source } = page();
    const marked = source.threads(REPORT);
    source.answered(1, { ok: threads[REPORT] });
    const rows = await marked;
    expect(rows.flatMap((row) => row.files.map((file) => file.landing))).toEqual(
      expect.arrayContaining(["landed", "redoing", "not_merged", "undone", null]),
    );
    // A later agent's mark, or what is no mark at all: the list is taken whole, that file with none.
    const later = source.threads(REPORT);
    const odd = ["kept_apart", "toString", "", 7, { mark: "landed" }];
    const row = threads[REPORT]!.find((found) => found.id === FIXTURE_IDS.idle)!;
    const files = [...odd.map((landing, at) => ({ ...row.files[0]!, ref: `odd-${at}.csv`, landing })), ...row.files];
    source.answered(2, { ok: threads[REPORT]!.map((found) => (found === row ? { ...row, files } : found)) });
    const taken = await later;
    expect(taken.map((found) => found.id)).toEqual(threads[REPORT]!.map((found) => found.id));
    expect(taken.find((found) => found.id === row.id)!.files.map((file) => [file.ref, file.landing])).toEqual([
      ...odd.map((_, at) => [`odd-${at}.csv`, null]), ...row.files.map((file) => [file.ref, file.landing]),
    ]);
  });

  it("take the rows of an agent older than the app, whose files carry no mark, with none", async () => {
    const { source } = page();
    const asked = source.threads(REPORT);
    // As a page built before file history maps a row: no landing on a file.
    const older = threads[REPORT]!.map(({ files, ...row }) => ({ ...row, files: files.map(({ landing: _mark, ...file }) => file) }));
    source.answered(1, { ok: older });
    const rows = await asked;
    expect(rows.map((row) => row.id)).toEqual(threads[REPORT]!.map((row) => row.id));
    expect(rows.flatMap((row) => row.files).length).toBeGreaterThan(0);
    expect(rows.every((row) => row.files.every((file) => file.landing === null))).toBe(true);
  });

  it("take a row of an agent newer than the app, leaving out what it does not know of", async () => {
    const { source } = page();
    const asked = source.threads(REPORT);
    const row = threads[REPORT]![0]!;
    source.answered(1, { ok: [{ ...row, landingId: "41", files: row.files.map((file) => ({ ...file, version: "41:f" })) }] });
    expect(await asked).toEqual([row]);
  });

  it("take a file's History, and refuse a version of another file, one with no zone or more than it lists", async () => {
    const { source, last } = page();
    const versions = history[REPORT]![REVENUE]!;
    const read = source.history(REPORT, REVENUE, { kind: "cloud" });
    expect(last()).toEqual({ type: "call", id: 1, method: "history", args: [REPORT, REVENUE, { kind: "cloud" }], deadline: expect.any(Number) });
    source.answered(1, { ok: versions.map((version) => ({ ...version, secret: "x" })) });
    expect(await read).toEqual(versions);
    const refused = "The agent's page answered history with something Surogate cannot use";
    const refusals: unknown[] = [
      [{ ...versions[0], path: "brief.docx" }],
      [{ ...versions[0], at: "2026-10-06T11:43:00" }],
      [{ ...versions[0], id: "" }],
      [{ ...versions[0], merged: "yes" }],
      [{ ...versions[0], available: 1 }],
      [{ ...versions[0], landingId: 9 }],
      Array.from({ length: 501 }, () => versions[0]),
      { versions },
    ];
    for (const [at, answer] of refusals.entries()) {
      const asked = source.history(REPORT, REVENUE, { kind: "cloud" });
      source.answered(at + 2, { ok: answer });
      await expect(asked, JSON.stringify(answer).slice(0, 80)).rejects.toThrow(refused);
    }
    // As many as the agent lists at most are taken.
    const full = source.history(REPORT, REVENUE, { kind: "cloud" });
    source.answered(refusals.length + 2, { ok: Array.from({ length: 500 }, () => versions[0]) });
    expect(await full).toHaveLength(500);
  });

  it("take a version by someone, or changed in a way, the app does not know as a plain version, never a refused list", async () => {
    const { source } = page();
    const [version] = history[REPORT]![REVENUE]!;
    const read = source.history(REPORT, REVENUE, { kind: "cloud" });
    // As the page of an agent newer than the app may serve it.
    source.answered(1, {
      ok: [
        { ...version, id: "20:f", by: { kind: "agent", name: "Reviewer" }, change: "merged_by_hand" },
        { ...version, id: "19:f", by: null, change: "restored" },
        { ...version, id: "18:f", by: { kind: "thread", threadId: "t-1" }, change: null },
        { ...version, id: "17:f", by: { kind: "routine", name: "n".repeat(501) }, change: "toString" },
        { ...version, id: "16:f", by: "constructor" },
        version,
      ],
    });
    expect((await read).map(({ id, by, change }) => [id, by, change])).toEqual([
      ["20:f", null, "changed"],
      ["19:f", null, "restored"],
      ["18:f", null, "changed"],
      ["17:f", null, "changed"],
      ["16:f", null, "changed"],
      ["12:f", version!.by, "changed"],
    ]);
  });

  it("give a History the plain bound of a read, and say the agent's own refusal of one", async () => {
    vi.useFakeTimers({ now: 1_000 });
    try {
      const { source, last } = page();
      const off = source.history(REPORT, REVENUE, { kind: "cloud" });
      expect(last()).toMatchObject({ method: "history", deadline: 1_000 + ANSWER_TIMEOUT_MS });
      source.answered(1, { error: "History is off: this project has more than 50,000 files." });
      await expect(off).rejects.toThrow("History is off: this project has more than 50,000 files.");
      const slow = source.history(REPORT, REVENUE, { kind: "cloud" });
      vi.advanceTimersByTime(ANSWER_TIMEOUT_MS);
      await expect(slow).rejects.toBeInstanceOf(TimedOut);
      await expect(slow).rejects.toThrow("The agent's page did not answer history in time");
    } finally {
      vi.useRealTimers();
    }
  });

  it("ask the page to open a version, with the time a whole file takes, and take nothing back of where it went", async () => {
    vi.useFakeTimers({ now: 1_000 });
    try {
      const { source, last } = page();
      const asked = { versionId: "9:f", path: REVENUE };
      const opened = source.openVersion(REPORT, asked);
      expect(last()).toEqual({ type: "call", id: 1, method: "openVersion", args: [REPORT, asked], deadline: 1_000 + LONG_ANSWER_TIMEOUT_MS });
      // The agent writes the version out and sends it whole, and the page reads it whole: past a plain call's bound.
      vi.advanceTimersByTime(ANSWER_TIMEOUT_MS + 5_000);
      source.answered(1, { ok: undefined });
      expect(await opened).toBeUndefined();
      // The page says only that it is done: a path on this computer, or anything else, is no answer of its.
      for (const [at, answer] of ["/home/flavius/Downloads/revenue.xlsx", { path: "revenue.xlsx" }, 0, false, ""].entries()) {
        const odd = source.openVersion(REPORT, asked);
        source.answered(at + 2, { ok: answer });
        await expect(odd).rejects.toThrow("The agent's page answered openVersion with something Surogate cannot use");
      }
      const gone = source.openVersion(REPORT, asked);
      source.answered(7, { error: "This version is no longer kept in the project's history." });
      await expect(gone).rejects.toThrow("This version is no longer kept in the project's history.");
      // Two minutes, and no longer.
      const slow = source.openVersion(REPORT, asked);
      let settled = false;
      void slow.catch(() => { settled = true; });
      await vi.advanceTimersByTimeAsync(LONG_ANSWER_TIMEOUT_MS - 1);
      expect(settled).toBe(false);
      await vi.advanceTimersByTimeAsync(1);
      await expect(slow).rejects.toBeInstanceOf(TimedOut);
      // A read keeps the plain bound, the deleted files' among them.
      void source.deleted(REPORT).catch(() => {});
      expect(last()).toMatchObject({ method: "deleted", deadline: Date.now() + ANSWER_TIMEOUT_MS });
    } finally {
      vi.useRealTimers();
    }
  });

  it("ask the page to restore a version, with the time a landing takes, and take what it did field by field", async () => {
    vi.useFakeTimers({ now: 1_000 });
    try {
      const { source, last } = page();
      const asked = { versionId: "9:f", path: REVENUE };
      const restored = source.restore(REPORT, asked);
      expect(last()).toEqual({ type: "call", id: 1, method: "restore", args: [REPORT, asked], deadline: 1_000 + LONG_ANSWER_TIMEOUT_MS });
      // The agent waits up to twenty seconds for the project's lock, then lands: past a plain call's bound.
      vi.advanceTimersByTime(ANSWER_TIMEOUT_MS + 15_000);
      const by = { kind: "thread", threadId: "t-1", title: "Draft A" };
      source.answered(1, { ok: { applied: [REVENUE], skipped: [{ path: "b.md", by, why: "x" }, { path: "c.md", by: null }], pickedUp: [REVENUE], secret: "x" } });
      expect(await restored).toEqual({ applied: [REVENUE], skipped: [{ path: "b.md", by }, { path: "c.md", by: null }], pickedUp: [REVENUE] });
      // Someone the app has no name for is no one it names: the answer is still taken.
      const later = source.restore(REPORT, asked);
      source.answered(2, { ok: { applied: [], skipped: [{ path: "b.md", by: { kind: "agent", name: "Reviewer" } }], pickedUp: [] } });
      expect(await later).toEqual({ applied: [], skipped: [{ path: "b.md", by: null }], pickedUp: [] });
      const refusals: unknown[] = [
        null, [], undefined, { applied: [REVENUE], skipped: [] }, { applied: REVENUE, skipped: [], pickedUp: [] },
        { applied: [7], skipped: [], pickedUp: [] }, { applied: ["a".repeat(4097)], skipped: [], pickedUp: [] },
        { applied: [], skipped: [{ by: null }], pickedUp: [] }, { applied: [], skipped: [null], pickedUp: [] },
        { applied: [], skipped: [], pickedUp: [null] },
        { applied: Array.from({ length: 2_001 }, (_, n) => `${n}.md`), skipped: [], pickedUp: [] },
      ];
      for (const [at, answer] of refusals.entries()) {
        const odd = source.restore(REPORT, asked);
        source.answered(at + 3, { ok: answer });
        await expect(odd, JSON.stringify(answer)?.slice(0, 80)).rejects.toThrow("The agent's page answered restore with something Surogate cannot use");
      }
      const busy = source.restore(REPORT, asked);
      source.answered(refusals.length + 3, { error: "Your project's files are being saved right now. Try again in a moment." });
      await expect(busy).rejects.toThrow("Your project's files are being saved right now. Try again in a moment.");
      // Two minutes, and no longer.
      const slow = source.restore(REPORT, asked);
      let settled = false;
      void slow.catch(() => { settled = true; });
      await vi.advanceTimersByTimeAsync(LONG_ANSWER_TIMEOUT_MS - 1);
      expect(settled).toBe(false);
      await vi.advanceTimersByTimeAsync(1);
      await expect(slow).rejects.toBeInstanceOf(TimedOut);
    } finally {
      vi.useRealTimers();
    }
  });

  it("take the project's deleted files and whether there are more, and refuse one that is no deletion or is listed twice", async () => {
    const { source, last } = page();
    const gone = source.deleted(REPORT);
    expect(last()).toEqual({ type: "call", id: 1, method: "deleted", args: [REPORT], deadline: expect.any(Number) });
    const listed = deleted[REPORT]!;
    source.answered(1, { ok: { files: listed.files.map((version) => ({ ...version, secret: "x" })), more: false, cursor: "x" } });
    expect(await gone).toEqual({ files: listed.files, more: false });
    const [version] = listed.files;
    const refusals: unknown[] = [
      listed.files,
      // A version that took no file away, of one file or of several, is no deleted file.
      { files: [history[REPORT]![REVENUE]![0]], more: false },
      { files: [version, { ...version, path: "kept.md", change: "added" }], more: false },
      { files: [version, version], more: false },
      { files: [{ ...version, at: "2026-10-06T11:15:00" }], more: false },
      { files: Array.from({ length: 501 }, (_, n) => ({ ...version, path: `${n}.md` })), more: true },
      { files: listed.files, more: "yes" },
      { files: listed.files },
      { more: false },
      null,
    ];
    for (const [at, answer] of refusals.entries()) {
      const asked = source.deleted(REPORT);
      source.answered(at + 2, { ok: answer });
      await expect(asked, JSON.stringify(answer)?.slice(0, 80)).rejects.toThrow("The agent's page answered deleted with something Surogate cannot use");
    }
    // As many as the agent lists at most are taken, and that there are more of them.
    const full = source.deleted(REPORT);
    source.answered(refusals.length + 2, { ok: { files: Array.from({ length: 500 }, (_, n) => ({ ...version, path: `${n}.md` })), more: true } });
    expect(await full).toMatchObject({ more: true, files: { length: 500 } });
  });
});
