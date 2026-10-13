import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
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
  let opened: string[] = [];

  beforeAll(async () => {
    fixture = await startFixture();
    pool = new PagePool({ hostUrl: fixture.hostUrl, maxPages: 2, idleMs: 600_000, openTimeoutMs: 60_000, commandTimeoutMs: 20_000, ignoreTls: true });
    office = new Office(pool);
  });
  afterEach(async () => {
    for (const id of opened) await pool.close(id);
    opened = [];
  });
  afterAll(async () => {
    await pool.shutdown();
    await fixture.close();
  });

  async function pair(): Promise<{ agent: string; user: string }> {
    const key = newKey();
    const agent = await office.open({ docKey: key, sessionId: "agent", config: editorConfig(key, AGENT), documentServerUrl: IT.dsUrl });
    const user = await office.open({ docKey: key, sessionId: "user", config: editorConfig(key, USER), documentServerUrl: IT.dsUrl });
    opened.push(agent, user);
    return { agent, user };
  }
  const comments = async (id: string): Promise<Any[]> => ((await office.read(id, { view: "outline" })) as Any).comments;
  const edit = async (id: string, ops: unknown[]): Promise<Any> => office.edit(id, compileOps(ops));
  // The user's editor can still be taking in the agent's change.
  async function eventually(check: () => Promise<void>, ms = 10_000): Promise<void> {
    const until = Date.now() + ms;
    for (;;) {
      try {
        return await check();
      } catch (error) {
        if (Date.now() > until) throw error;
        await new Promise((resolve) => setTimeout(resolve, 250));
      }
    }
  }

  it("comments on words, replies and resolves, under the agent's name", async () => {
    const { agent, user } = await pair();
    const made = await edit(agent, [{ op: "comment", anchor: { find: "North grew" }, body: "Source?" }]);
    const thread = made.results[0].thread;
    expect(thread).toMatch(/\S/);
    await edit(agent, [{ op: "reply", thread, body: "Added below." }, { op: "resolve", thread }]);
    await eventually(async () => {
      expect(await comments(user)).toEqual([{ id: thread, author: "Agent", quote: "North grew", text: "Source?", replies: 1, resolved: true }]);
    });
    const reply = await (await pool.get(user)).command(
      "function () { var r = Api.GetDocument().GetAllComments()[0].GetReply(0); return { author: r.GetAuthorName(), user: r.GetUserId(), text: r.GetText() }; }",
      {},
    );
    expect(reply.value).toEqual({ author: "Agent", user: AGENT.id, text: "Added below." });
    const gone = await edit(agent, [{ op: "reply", thread: "999999", body: "x" }]);
    expect(gone.refused.reason).toMatch(/is gone/);
  });

  it("keeps no reply or resolve of a refused batch, in the agent's editor or the user's", async () => {
    const { agent, user } = await pair();
    const thread = (await edit(agent, [{ op: "comment", anchor: { find: "North grew" }, body: "Source?" }])).results[0].thread;
    const replied = await edit(agent, [{ op: "reply", thread, body: "x" }, { op: "reply", thread: "999999", body: "y" }]);
    expect(replied).toMatchObject({ applied: false, refused: { op: 1 } });
    expect(replied.refused.reason).toMatch(/is gone/);
    const resolved = await edit(agent, [{ op: "resolve", thread }, { op: "resolve", thread: "999999" }]);
    expect(resolved).toMatchObject({ applied: false, refused: { op: 1 } });
    expect(resolved.refused.reason).toMatch(/is gone/);
    const untouched = { id: thread, author: "Agent", quote: "North grew", text: "Source?", replies: 0, resolved: false };
    expect(await comments(agent)).toEqual([untouched]);
    await eventually(async () => expect(await comments(user)).toEqual([untouched]));
  });
});
