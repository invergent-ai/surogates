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
    expect(last()).toEqual({ type: "call", id: 1, method: "list", args: [] });
    source.answered(1, { ok: projects.map((project) => ({ ...project, secret: "x" })) });
    const summaries = await listed;
    expect(summaries[0]).toEqual({
      id: REPORT, name: "Quarterly report", icon: null, createdAt: projects[0]!.createdAt, updatedAt: projects[0]!.updatedAt,
      waiting: 3, working: 2,
    });
    const rows = source.threads(REPORT);
    expect(last()).toEqual({ type: "call", id: 2, method: "threads", args: [REPORT] });
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
