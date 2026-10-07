import { describe, expect, it, vi } from "vitest";

import { FIXTURE_IDS, projectFixtures } from "../../web/src/lib/projects.js";
import { PageProjects, type ToPage } from "../src/shell/projects.js";

const { projects, threads } = projectFixtures(Date.parse("2026-10-06T12:00:00Z"));
const REPORT = FIXTURE_IDS.report;

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
});
