// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Sub-agent + scheduled sessions are read-only.  These tests pin the
// classifier so adding a new harness-managed child-session channel
// (or accidentally removing one) fails fast instead of silently
// re-enabling the composer in a thread the parent's LLM is the only
// authoritative voice for.

import { describe, expect, it } from "vitest";

import {
  isScheduledRunSession,
  isSubAgentSession,
  readOnlyReasonForSession,
} from "../src/lib/sessions";
import type { AgentChatSession } from "../src/types";

function makeSession(overrides: Partial<AgentChatSession>): AgentChatSession {
  return {
    id: "s-1",
    status: "active",
    ...overrides,
  };
}

describe("isScheduledRunSession", () => {
  it("matches channel='scheduled'", () => {
    expect(isScheduledRunSession(makeSession({ channel: "scheduled" }))).toBe(
      true,
    );
  });

  it("matches the scheduled_session_id config flag", () => {
    expect(
      isScheduledRunSession(
        makeSession({ config: { scheduled_session_id: "abc" } }),
      ),
    ).toBe(true);
  });

  it("rejects a plain web session", () => {
    expect(isScheduledRunSession(makeSession({ channel: "web" }))).toBe(false);
  });
});

describe("isSubAgentSession", () => {
  it.each(["delegation", "worker", "task"])(
    "matches channel=%s",
    (channel) => {
      expect(isSubAgentSession(makeSession({ channel }))).toBe(true);
    },
  );

  it("matches any session with a parentId regardless of channel", () => {
    // parentId is the canonical signal -- channels are an additional
    // safety net for child rows that get re-channelled downstream.
    expect(
      isSubAgentSession(makeSession({ channel: "web", parentId: "parent-1" })),
    ).toBe(true);
  });

  it("rejects a root session", () => {
    expect(isSubAgentSession(makeSession({ channel: "web" }))).toBe(false);
    expect(isSubAgentSession(makeSession({ channel: "api" }))).toBe(false);
    expect(isSubAgentSession(makeSession({ channel: "telegram" }))).toBe(false);
  });

  it("returns false for null/undefined", () => {
    expect(isSubAgentSession(null)).toBe(false);
    expect(isSubAgentSession(undefined)).toBe(false);
  });
});

describe("readOnlyReasonForSession", () => {
  it("returns the scheduled-run reason when applicable", () => {
    const out = readOnlyReasonForSession(
      makeSession({ channel: "scheduled" }),
    );
    expect(out.readOnly).toBe(true);
    expect(out.reason).toBe("Scheduled run is read-only");
  });

  it("returns the sub-agent reason for delegated children", () => {
    const out = readOnlyReasonForSession(
      makeSession({ channel: "delegation" }),
    );
    expect(out.readOnly).toBe(true);
    expect(out.reason).toContain("Sub-task");
  });

  it("returns the sub-agent reason for parented children", () => {
    const out = readOnlyReasonForSession(
      makeSession({ channel: "web", parentId: "p-1" }),
    );
    expect(out.readOnly).toBe(true);
    expect(out.reason).toContain("Sub-task");
  });

  it("prefers the scheduled-run reason when both signals apply", () => {
    // A scheduled run that was also spawned as a sub-agent (edge
    // case) should still report the scheduled-run label, since
    // scheduled is the more specific user-facing concept.
    const out = readOnlyReasonForSession(
      makeSession({ channel: "scheduled", parentId: "p-1" }),
    );
    expect(out.readOnly).toBe(true);
    expect(out.reason).toBe("Scheduled run is read-only");
  });

  it("returns {readOnly: false} for a normal user thread", () => {
    const out = readOnlyReasonForSession(makeSession({ channel: "web" }));
    expect(out.readOnly).toBe(false);
    expect(out.reason).toBeUndefined();
  });
});
