import { describe, expect, it } from "vitest";

import { linkIn } from "../src/shell/deep-link.js";

const CHAT = "7d2e0f8a-2b3c-4d5e-9f60-718293a4b5c6";
const link = (url: string) => `surogate://open?url=${encodeURIComponent(url)}`;

describe("a surogate:// link", () => {
  it("opens the agent it names, at a page of its web client when it names one", () => {
    expect(linkIn(["/opt/surogate/surogate", link("https://agent.example.com")])).toEqual({ origin: "https://agent.example.com", path: "/" });
    expect(linkIn(["electron", "--ozone-platform=x11", "dist/shell/main.js", link(`https://agent.example.com/chat/${CHAT}`)]))
      .toEqual({ origin: "https://agent.example.com", path: `/chat/${CHAT}` });
    expect(linkIn([link("agent.example.com/inbox")])).toEqual({ origin: "https://agent.example.com", path: "/inbox" });
    expect(linkIn(["SUROGATE://OPEN/?url=https%3A%2F%2Fagent.example.com"])).toEqual({ origin: "https://agent.example.com", path: "/" });
    expect(linkIn([link("http://127.0.0.1:8000/chat")])).toEqual({ origin: "http://127.0.0.1:8000", path: "/chat" });
  });

  it("opens no other page than the web client's own", () => {
    expect(linkIn([link("https://agent.example.com/oauth/authorize?client_id=x")])).toEqual({ origin: "https://agent.example.com", path: "/" });
    expect(linkIn([link("https://agent.example.com/chat/../settings")])).toEqual({ origin: "https://agent.example.com", path: "/settings" });
  });

  it("is no link when it names no agent the first run would take, asks for anything but open, or is too long", () => {
    for (const refused of [
      link("https://user:secret@agent.example.com"),
      link("http://agent.example.com"),
      link("file:///home/flavius"),
      "surogate://open",
      "surogate://open/chat?url=https%3A%2F%2Fagent.example.com",
      "surogate://grant?folder=%2Fhome%2Fflavius",
      `surogate://open?url=https%3A%2F%2Fagent.example.com%2F${"a".repeat(2048)}`,
      "surogate:not a link",
    ]) expect(linkIn(["surogate", refused]), refused).toBeNull();
    expect(linkIn(["/opt/surogate/surogate", "--password-store=basic"])).toBeNull();
  });
});
