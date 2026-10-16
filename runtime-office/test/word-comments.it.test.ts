import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { Office } from "../src/office.js";
import { PagePool } from "../src/pages.js";
import { AGENT, USER, eventually, itEnabled, pair, startFixture, type Fixture } from "./support/office.js";

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

  // The person's own comment on words, made in their editor, and the thread id the editor's mention of it carries: its
  // durable id in hex.
  async function personComments(user: string, text: string): Promise<string> {
    const made = await (await pool.get(user)).command(
      "function () { var c = Api.GetDocument().Search(scope.anchor)[0].AddComment(scope.text, scope.name, scope.id);" +
        " var h = Number(c.GetId()).toString(16); return h.length < 8 ? ('0000000' + h).slice(-8) : h; }",
      { anchor: "North grew", text, name: USER.name, id: USER.id },
    );
    return made.value as string;
  }

  it("names a thread as the person's mention does, and answers it there", async () => {
    const { agent, user } = await pair(office, opened);
    const thread = await personComments(user, "@Agent is this the March figure?");
    await (await pool.get(user)).command(
      "function () { Api.GetDocument().GetAllComments()[0].AddReply(scope.text, scope.name, scope.id); return true; }",
      { text: "and the east?", name: USER.name, id: USER.id },
    );
    await eventually(async () => {
      expect(await comments(agent)).toEqual([{ id: thread, author: "Flavius", quote: "North grew", text: "@Agent is this the March figure?",
        replies: [{ author: "Flavius", text: "and the east?" }], resolved: false }]);
    });
    await edit(agent, [{ op: "reply", thread, body: "Yes: March, from the regional export." }, { op: "resolve", thread }]);
    await eventually(async () => {
      expect((await comments(user))[0]).toMatchObject({ id: thread, resolved: true, replies: [
        { author: "Flavius", text: "and the east?" }, { author: "Agent", text: "Yes: March, from the regional export." }] });
    });
  });

  it("refuses a long thread reply before anything applies, and the shorter retry lands once", async () => {
    const { agent, user } = await pair(office, opened);
    const thread = await personComments(user, "@Agent check this");
    await eventually(async () => expect((await comments(agent)).map((c: Any) => c.id)).toEqual([thread]));
    await expect(edit(agent, [{ op: "reply", thread, body: "x".repeat(401) }])).rejects.toThrow("a thread reply states just the result");
    expect((await comments(agent))[0].replies).toEqual([]);
    await edit(agent, [{ op: "reply", thread, body: "Checked: it is right." }]);
    await eventually(async () => expect((await comments(user))[0].replies).toEqual([{ author: "Agent", text: "Checked: it is right." }]));
  });

  it("comments on words, replies and resolves, under the agent's name", async () => {
    const { agent, user } = await pair(office, opened);
    const made = await edit(agent, [{ op: "comment", anchor: { find: "North grew" }, body: "Source?" }]);
    const thread = made.results[0].thread;
    expect(thread).toMatch(/^[0-9a-f]{8,}$/);
    await edit(agent, [{ op: "reply", thread, body: "Added below." }, { op: "resolve", thread }]);
    await eventually(async () => {
      expect(await comments(user)).toEqual([{ id: thread, author: "Agent", quote: "North grew", text: "Source?",
        replies: [{ author: "Agent", text: "Added below." }], resolved: true }]);
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
    const untouched = { id: thread, author: "Agent", quote: "North grew", text: "Source?", replies: [], resolved: false };
    expect(await comments(agent)).toEqual([untouched]);
    await eventually(async () => expect(await comments(user)).toEqual([untouched]));
  });
});
