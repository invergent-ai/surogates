import { describe, expect, it } from "vitest";

import { ownPage, permitted, sameOrigin, webClientPath, windowOpen } from "../src/shell/window-policy.js";

const AGENT = "https://agent.example.com";

describe("an agent window", () => {
  it.each([
    ["https://agent.example.com/chat/1", true],
    ["https://agent.example.com:443/login", true],
    ["https://agent.example.com.evil.com/", false],
    ["http://agent.example.com/", false],
    ["https://other.example.com/", false],
    ["about:blank", false],
    ["not a url", false],
  ])("stays on its origin: %s is %s", (url, stays) => {
    expect(sameOrigin(AGENT, url)).toBe(stays);
  });

  it.each([
    ["https://connect.composio.dev/link/abc", "composio-oauth", "popup"],
    ["https://evil.example.com/", "composio-oauth", "external"],
    ["https://connect.composio.dev/link/abc", "", "external"],
    ["https://docs.example.com/page", "_blank", "external"],
    ["http://docs.example.com/page", "", "external"],
    ["javascript:alert(1)", "", "deny"],
    ["file:///etc/passwd", "", "deny"],
    ["surogates://open?url=https://x", "", "deny"],
    ["not a url", "", "deny"],
  ] as const)("opens %s (%s) as %s", (url, frameName, action) => {
    expect(windowOpen(url, frameName)).toBe(action);
  });

  it.each([
    ["notifications", "https://agent.example.com/chat", true],
    ["clipboard-sanitized-write", "https://agent.example.com/", true],
    ["media", "https://agent.example.com/", false],
    ["geolocation", "https://agent.example.com/", false],
    // A frame from elsewhere in the agent's page, such as an embedded widget, gets nothing.
    ["notifications", "https://widget.example.net/frame", false],
    ["clipboard-sanitized-write", "https://agent.example.com.evil.com/", false],
    ["notifications", "", false],
  ])("grants %s to %s: %s", (permission, requestingUrl, granted) => {
    expect(permitted(AGENT, permission, requestingUrl)).toBe(granted);
  });
});

describe("where the sidebar may take the web client", () => {
  it.each([
    ["/chat", true],
    ["/chat/0b6f3c1e-8a2d-4c5e-9f10-1a2b3c4d5e6f", true],
    ["/inbox", true],
    ["/missions", true],
    ["/skills", true],
    ["/settings", true],
    ["/chat/../admin", false],
    ["//evil.example.com", false],
    ["https://evil.example.com/chat", false],
    ["/login", false],
    ["/chat/not-a-session", false],
  ])("%s is %s", (path, allowed) => {
    expect(webClientPath(path)).toBe(allowed);
  });
});

describe("one of the app's own pages", () => {
  const PAGE = "/opt/surogate/dist/shell/pages/prompt.html";

  it.each([
    ["its own top frame", { url: "file:///opt/surogate/dist/shell/pages/prompt.html", parent: null }, true],
    ["another of the app's pages", { url: "file:///opt/surogate/dist/shell/pages/shell.html", parent: null }, false],
    ["a file dropped on it", { url: "file:///home/me/Downloads/prompt.html", parent: null }, false],
    ["a frame inside it", { url: "file:///opt/surogate/dist/shell/pages/prompt.html", parent: {} }, false],
    ["a web page", { url: "https://agent.example.com/prompt.html", parent: null }, false],
    ["a frame that has gone", null, false],
  ])("is %s: %s", (_name, frame, own) => {
    expect(ownPage(frame, PAGE)).toBe(own);
  });
});
