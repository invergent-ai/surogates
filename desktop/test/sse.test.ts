import { describe, expect, it } from "vitest";

import { readEvents, type ServerEvent } from "../src/shell/sse.js";

async function read(chunks: string[]): Promise<{ events: ServerEvent[]; heard: number }> {
  const events: ServerEvent[] = [];
  let heard = 0;
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(new TextEncoder().encode(chunk));
      controller.close();
    },
  });
  await readEvents(body, (event) => events.push(event), () => heard++);
  return { events, heard };
}

describe("server-sent events", () => {
  it("reads each event's id, type and data, whatever the chunks split", async () => {
    const { events, heard } = await read([
      ": connected\r\n\r\nevent: snap", "shot\r\ndata: {\"unread_ids\": []}\r", "\n\r\nid: 41\r\nevent: item\r\ndata: one\r\ndata: two\r\n",
      "\r\ndata: plain\r\n\r\n",
    ]);
    expect(events).toEqual([
      { id: null, type: "snapshot", data: "{\"unread_ids\": []}" },
      { id: "41", type: "item", data: "one\ntwo" },
      { id: "41", type: "message", data: "plain" },
    ]);
    expect(heard).toBe(4);
  });

  it("tells a comment as heard, and no event", async () => {
    expect(await read([": ping - 2026-10-07\r\n\r\n"])).toEqual({ events: [], heard: 1 });
  });

  it("gives up on a line, or an event's data, longer than a mebibyte, as no agent sends one", async () => {
    await expect(read(["data: ", "x".repeat(1 << 20)])).rejects.toThrow("The agent sent a line or an event longer than 1 MiB");
    const lines = `data: ${"x".repeat(1_000)}\r\n`.repeat(1_100);
    await expect(read([lines])).rejects.toThrow("The agent sent a line or an event longer than 1 MiB");
    // Each well under it, the same lines as events of their own are read.
    expect((await read([lines.replaceAll("\r\n", "\r\n\r\n")])).events).toHaveLength(1_100);
  });

  it("leaves an event the stream ended in the middle of untold", async () => {
    expect((await read(["event: item\r\ndata: cut"])).events).toEqual([]);
  });
});
