import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { Office } from "../src/office.js";
import { PagePool } from "../src/pages.js";
import { AGENT, eventually, itEnabled, pair, startFixture, type Fixture } from "./support/office.js";

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

  const comments = async (id: string): Promise<Any[]> => ((await office.read(id, { view: "outline" })) as Any).comments;
  const edit = async (id: string, ops: unknown[]): Promise<Any> => office.edit(id, ops);

  it("comments on words, replies and resolves, under the agent's name", async () => {
    const { agent, user } = await pair(office, opened);
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
    const { agent, user } = await pair(office, opened);
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
