import { describe, expect, it } from "vitest";
import { SessionEventLog } from "../src/server/events";

describe("SessionEventLog", () => {
  it("assigns monotonic ids and replays events after a cursor", () => {
    const log = new SessionEventLog();
    const first = log.append("session.start", { session_id: "s-1" });
    const second = log.append("user.message", { content: "hello" });
    const third = log.append("llm.delta", { content: "hi" });

    expect(first.eventId).toBe(1);
    expect(second.eventId).toBe(2);
    expect(third.eventId).toBe(3);
    expect(log.replay(1)).toEqual([second, third]);
  });

  it("delivers appended events to subscribers until unsubscribed", () => {
    const log = new SessionEventLog();
    const seen: string[] = [];
    const unsubscribe = log.subscribe((event) => seen.push(event.type));

    log.append("harness.wake");
    unsubscribe();
    log.append("session.done");

    expect(seen).toEqual(["harness.wake"]);
  });
});
