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

  // A command in the person's editor. Right after a co-editor's change arrives the editor skips commands, answering
  // nothing and changing nothing: it is tried again, as the agent's are.
  async function asPerson(user: string, source: string, scope: object): Promise<{ value: unknown }> {
    for (let attempt = 0; ; attempt++) {
      const reply = await (await pool.get(user)).command(source, scope);
      if (reply.value !== undefined || attempt === 8) return reply;
      await new Promise((resolve) => setTimeout(resolve, 300));
    }
  }

  // The person's own comment on words, made in their editor, and the thread id the editor's mention of it carries: its
  // durable id in hex.
  async function personComments(user: string, text: string): Promise<string> {
    const made = await asPerson(user,
      "function () { var c = Api.GetDocument().Search(scope.anchor)[0].AddComment(scope.text, scope.name, scope.id);" +
        " var h = Number(c.GetId()).toString(16); return h.length < 8 ? ('0000000' + h).slice(-8) : h; }",
      { anchor: "North grew", text, name: USER.name, id: USER.id },
    );
    return made.value as string;
  }

  const ACK = "On it, I'll reply here when done.";
  const blockWith = async (id: string, words: string): Promise<Any> => ((await office.read(id, { view: "search", query: words })) as Any).matches[0];

  // The words a thread is on, in that page's editor. No builder call tells it: the editor's own selection of a comment's
  // text does.
  async function covered(id: string, thread: string): Promise<unknown> {
    const read = await (await pool.get(id)).command(
      "function () { var doc = Api.GetDocument(), logic = doc.Document, c = doc.GetCommentById(String(parseInt(scope.thread, 16)));" +
        " if (!c || !c.Comment.SelectCommentText()) return null; var text = logic.GetSelectedText(false); logic.RemoveSelection(); return text; }",
      { thread },
    );
    return read.value;
  }

  // The person's comment on "North grew", acknowledged by the agent, as a mention leaves it.
  async function acknowledged(agent: string, user: string): Promise<string> {
    const thread = await personComments(user, "@Agent the north figure?");
    await eventually(async () => expect((await comments(agent)).map((c: Any) => c.id)).toEqual([thread]));
    expect(await edit(agent, [{ op: "reply", thread, body: ACK }])).toMatchObject({ applied: true });
    return thread;
  }

  // The person's thread as an outline lists it, with the agent's replies.
  const listed = (id: string, replies: string[]) => ({ id, author: "Flavius", quote: "North grew", text: "@Agent the north figure?",
    replies: replies.map((text) => ({ author: "Agent", text })), resolved: false });

  it("names a thread as the person's mention does, and answers it there", async () => {
    const { agent, user } = await pair(office, opened);
    const thread = await personComments(user, "@Agent is this the March figure?");
    await asPerson(user,
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

  it("bounds a thread's words by characters too, when they have no spaces", async () => {
    const { agent, user } = await pair(office, opened);
    const thread = await personComments(user, "x".repeat(400));
    await asPerson(user,
      "function () { Api.GetDocument().GetAllComments()[0].AddReply(scope.text, scope.name, scope.id); return true; }",
      { text: "y".repeat(400), name: USER.name, id: USER.id },
    );
    await eventually(async () => {
      expect(await comments(agent)).toEqual([{ id: thread, author: "Flavius", quote: "North grew", text: `${"x".repeat(300)} …`,
        replies: [{ author: "Flavius", text: `${"y".repeat(300)} …` }], resolved: false }]);
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
    expect(gone.refused.reason).toBe("Comment thread 999999 is gone: its comment was deleted, or the text it was on was removed. Read the outline again.");
  });

  it("keeps a thread on the words a replace rewrites, and answers it there", async () => {
    const { agent, user } = await pair(office, opened);
    const id = await acknowledged(agent, user);
    const north = await blockWith(agent, "North grew");
    const replaced = await edit(agent, [{ op: "replace", target: { block: north.id, hash: north.hash }, content: { markdown: "North grew fastest (12%), then the east." } }]);
    expect(replaced).toMatchObject({ applied: true });
    for (const page of [agent, user]) {
      await eventually(async () => {
        expect(await comments(page)).toEqual([listed(id, [ACK])]);
        expect(await covered(page, id)).toBe("North grew");
        expect((await blockWith(page, "North grew")).text).toBe("North grew fastest (12%), then the east.");
      });
    }
    expect(await edit(agent, [{ op: "reply", thread: id, body: "North grew 12%." }, { op: "resolve", thread: id }])).toMatchObject({ applied: true });
    await eventually(async () => expect(await comments(user)).toEqual([{ ...listed(id, [ACK, "North grew 12%."]), resolved: true }]));
  });

  it("keeps a thread over the new block when its words are gone, and a reply in the same call lands", async () => {
    const { agent, user } = await pair(office, opened);
    const id = await acknowledged(agent, user);
    const north = await blockWith(agent, "North grew");
    const replaced = await edit(agent, [
      { op: "replace", target: { block: north.id, hash: north.hash }, content: { markdown: "The north led growth." } },
      { op: "reply", thread: id, body: "Rewritten: the north led." },
    ]);
    expect(replaced).toMatchObject({ applied: true });
    for (const page of [agent, user]) {
      await eventually(async () => {
        expect(await comments(page)).toEqual([listed(id, [ACK, "Rewritten: the north led."])]);
        expect(await covered(page, id)).toBe("The north led growth.");
      });
    }
  });

  it("keeps a thread in a block it moves", async () => {
    const { agent, user } = await pair(office, opened);
    const id = await acknowledged(agent, user);
    const north = await blockWith(agent, "North grew");
    const first = ((await office.read(agent, { view: "outline" })) as Any).blocks[0];
    const moved = await edit(agent, [{ op: "move", target: { block: north.id, hash: north.hash }, to: { block: first.id, hash: first.hash }, side: "before" }]);
    expect(moved).toMatchObject({ applied: true });
    for (const page of [agent, user]) {
      await eventually(async () => {
        expect(await comments(page)).toEqual([listed(id, [ACK])]);
        expect(await covered(page, id)).toBe("North grew");
        expect(((await office.read(page, { view: "outline" })) as Any).blocks[0].text).toBe("North grew fastest, then the east.");
      });
    }
  });

  it("takes back a kept thread with the rest of a refused batch", async () => {
    const { agent, user } = await pair(office, opened);
    const id = await acknowledged(agent, user);
    const north = await blockWith(agent, "North grew");
    const refused = await edit(agent, [
      { op: "replace", target: { block: north.id, hash: north.hash }, content: { markdown: "The north led growth." } },
      { op: "reply", thread: "999999", body: "x" },
    ]);
    expect(refused).toMatchObject({ applied: false, refused: { op: 1 } });
    for (const page of [agent, user]) {
      await eventually(async () => {
        expect(await comments(page)).toEqual([listed(id, [ACK])]);
        expect(await covered(page, id)).toBe("North grew");
        expect(await blockWith(page, "North grew")).toMatchObject({ id: north.id, hash: north.hash });
      });
    }
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
