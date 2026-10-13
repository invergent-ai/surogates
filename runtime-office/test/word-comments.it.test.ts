import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { compileOps } from "../src/ops.js";
import { Office } from "../src/office.js";
import { PagePool } from "../src/pages.js";
import { AGENT, editorConfig, IT, itEnabled, newKey, startFixture, USER, type Fixture } from "./support/office.js";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Any = any;

describe.skipIf(!itEnabled)("comments", () => {
  let fixture: Fixture;
  let pool: PagePool;
  let office: Office;

  beforeAll(async () => {
    fixture = await startFixture();
    pool = new PagePool({ hostUrl: fixture.hostUrl, maxPages: 2, idleMs: 600_000, openTimeoutMs: 60_000, commandTimeoutMs: 20_000, ignoreTls: true });
    office = new Office(pool);
  });
  afterAll(async () => {
    await pool.shutdown();
    await fixture.close();
  });

  it("comments on words, replies and resolves, under the agent's name", async () => {
    const key = newKey();
    const agent = await office.open({ docKey: key, sessionId: "agent", config: editorConfig(key, AGENT), documentServerUrl: IT.dsUrl });
    const user = await office.open({ docKey: key, sessionId: "user", config: editorConfig(key, USER), documentServerUrl: IT.dsUrl });
    const made: Any = await office.edit(agent, compileOps([{ op: "comment", anchor: { find: "North grew" }, body: "Source?" }]));
    const thread = made.results[0].thread;
    expect(thread).toMatch(/\S/);
    await office.edit(agent, compileOps([{ op: "reply", thread, body: "Added below." }, { op: "resolve", thread }]));
    const deadline = Date.now() + 10_000;
    let comments: Any[] = [];
    while (Date.now() < deadline) {
      try {
        comments = ((await office.read(user, { view: "outline" })) as Any).comments;
        if (comments[0]?.resolved) break;
      } catch {
        // The user's editor can still be taking in the agent's change.
      }
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
    expect(comments).toEqual([{ id: thread, author: "Agent", quote: "North grew", text: "Source?", replies: 1, resolved: true }]);
    const gone: Any = await office.edit(agent, compileOps([{ op: "reply", thread: "999999", body: "x" }]));
    expect(gone.refused.reason).toMatch(/is gone/);
  });
});
