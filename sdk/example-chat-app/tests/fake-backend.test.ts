// @vitest-environment node
import { afterEach, describe, expect, it } from "vitest";
import type { Server } from "node:http";
import type { AddressInfo } from "node:net";
import { createExampleApp } from "../src/server/app";
import { fakeReply, streamReply } from "../src/server/fake-llm";
import { ExampleSessionStore } from "../src/server/session-store";

let server: Server | null = null;

afterEach(() => {
  server?.close();
  server = null;
});

async function start(chunkDelayMs: number) {
  const store = new ExampleSessionStore();
  const app = createExampleApp({ store, chunkDelayMs });
  server = app.listen(0);
  const baseUrl = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  const record = store.create();
  const post = (path: string, body: unknown = {}) =>
    fetch(`${baseUrl}/api/sessions/${record.session.id}/${path}`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
  const types = () => record.events.replay().map((event) => event.type);
  return { record, post, types };
}

async function until(done: () => boolean) {
  for (let waited = 0; !done(); waited += 5) {
    if (waited > 2000) throw new Error("timed out");
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
}

describe("fake backend", () => {
  it("streams the reply in chunks that make up the whole reply", async () => {
    const reply = fakeReply("hello");
    let joined = "";
    for await (const chunk of streamReply(reply, 0)) joined += chunk;
    expect(joined).toBe(reply);
  });

  it("answers a message, streams it, and names the session after it", async () => {
    const { record, post, types } = await start(0);

    await post("messages", { content: "What is a harness?" });
    await until(() => types().includes("session.done"));

    const events = record.events.replay();
    const deltas = events.filter((event) => event.type === "llm.delta");
    const response = events.find((event) => event.type === "llm.response");
    expect(deltas.map((event) => event.data.content).join("")).toBe(
      (response?.data.message as { content: string }).content,
    );
    expect(types().slice(-2)).toEqual(["llm.response", "session.done"]);
    expect(record.session.title).toBe("What is a harness?");
  });

  it("ends a turn at Stop, and the next turn is whole", async () => {
    const { record, post, types } = await start(5);

    await post("messages", { content: "first" });
    await until(() => types().includes("llm.delta"));
    await post("pause");
    const stoppedAt = record.events.replay().length;
    await new Promise((resolve) => setTimeout(resolve, 100));

    expect(record.events.replay().length).toBe(stoppedAt);
    expect(types()).not.toContain("llm.response");

    await post("retry");
    await until(() => types().includes("llm.response"));
    expect(types().filter((type) => type === "llm.response")).toHaveLength(1);
  });

  it("records a rating as a user.feedback event on the reply", async () => {
    const { record, post } = await start(0);

    const response = await post("feedback", {
      llmResponseEventId: 9,
      rating: "down",
      reason: "too long",
    });

    expect(response.status).toBe(200);
    expect(record.events.replay().at(-1)).toMatchObject({
      type: "user.feedback",
      data: { source: "user", target_event_id: 9, rating: "down", reason: "too long" },
    });
  });
});
