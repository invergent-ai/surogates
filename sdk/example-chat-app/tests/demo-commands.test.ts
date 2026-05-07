import { describe, expect, it } from "vitest";
import {
  DEMO_SLASH_COMMANDS,
  HARNESS_BUILTIN_TOOL_NAMES,
  runDemoCommand,
} from "../src/server/demo-commands";
import { ExampleSessionStore } from "../src/server/session-store";

describe("demo commands", () => {
  it("lists documented slash commands", () => {
    expect(DEMO_SLASH_COMMANDS.map((command) => command.value)).toEqual([
      "/demo-tools",
      "/demo-artifacts",
      "/demo-clarify",
      "/demo-expert",
      "/demo-errors",
      "/demo-context",
    ]);
  });

  it("emits tool call and result events", () => {
    const store = new ExampleSessionStore();
    const record = store.create();

    expect(runDemoCommand(store, record, "/demo-tools")).toBe(true);

    const types = record.events.replay().map((event) => event.type);
    expect(types).toContain("tool.call");
    expect(types).toContain("tool.result");
  });

  it("emits demo tool payloads that match specialized agent-chat-react renderers", () => {
    const store = new ExampleSessionStore();
    const record = store.create();

    runDemoCommand(store, record, "/demo-tools");

    const events = record.events.replay();
    const calls = events.filter((event) => event.type === "tool.call");
    const results = events.filter((event) => event.type === "tool.result");
    const terminalCall = calls.find(
      (event) => event.data.tool_call_id === "demo-terminal",
    );
    const terminalResult = results.find(
      (event) => event.data.tool_call_id === "demo-terminal",
    );
    const memoryCall = calls.find(
      (event) => event.data.tool_call_id === "demo-memory",
    );
    const memoryResult = results.find(
      (event) => event.data.tool_call_id === "demo-memory",
    );

    expect(terminalCall?.data).toMatchObject({
      name: "terminal",
      arguments: { command: "pnpm test" },
    });
    expect(JSON.parse(String(terminalResult?.data.content))).toMatchObject({
      output: "5 test files passed in the example app.",
      exit_code: 0,
      error: null,
    });
    expect(memoryCall?.data).toMatchObject({
      name: "memory",
      arguments: {
        action: "add",
        target: "memory",
        content: "The user prefers concise examples.",
      },
    });
    expect(JSON.parse(String(memoryResult?.data.content))).toMatchObject({
      success: true,
      message: "Memory stored for this demo session.",
    });
  });

  it("covers every built-in harness tool with scripted demo events", () => {
    const store = new ExampleSessionStore();
    const record = store.create();

    for (const command of DEMO_SLASH_COMMANDS) {
      if (command.value === "/demo-context" || command.value === "/demo-errors") continue;
      runDemoCommand(store, record, command.value);
    }

    const emittedToolNames = new Set(
      record.events
        .replay()
        .filter((event) => event.type === "tool.call")
        .map((event) => String(event.data.name)),
    );

    expect([...emittedToolNames].sort()).toEqual(
      [...new Set([...HARNESS_BUILTIN_TOOL_NAMES, "execute_code"])].sort(),
    );
  });

  it("emits a named expert payload for the expert renderer", () => {
    const store = new ExampleSessionStore();
    const record = store.create();

    runDemoCommand(store, record, "/demo-expert");

    const expertCall = record.events.replay().find(
      (event) =>
        event.type === "tool.call" &&
        event.data.tool_call_id === "demo-expert",
    );

    expect(expertCall?.data).toMatchObject({
      name: "consult_expert",
      arguments: {
        expert: "Architecture reviewer",
        question: "Review the example app architecture.",
      },
    });
  });

  it("creates retrievable artifact payloads", () => {
    const store = new ExampleSessionStore();
    const record = store.create();

    runDemoCommand(store, record, "/demo-artifacts");

    const artifactEvents = record.events
      .replay()
      .filter((event) => event.type === "artifact.created");
    expect(artifactEvents).toHaveLength(5);
    const artifactId = String(artifactEvents[0]?.data.artifact_id);
    expect(store.getArtifact(record, artifactId).meta.artifact_id).toBe(artifactId);
  });

  it("emits clarify, expert, error, and context events", () => {
    const store = new ExampleSessionStore();
    const record = store.create();

    runDemoCommand(store, record, "/demo-clarify");
    runDemoCommand(store, record, "/demo-expert");
    runDemoCommand(store, record, "/demo-errors");
    runDemoCommand(store, record, "/demo-context");

    const types = record.events.replay().map((event) => event.type);
    expect(types).toContain("tool.call");
    expect(types).toContain("expert.result");
    expect(types).toContain("policy.denied");
    expect(types).toContain("session.fail");
    expect(types).toContain("context.compact");
  });
});
