import { describe, expect, it } from "vitest";
import { readableOutput, toolFailure } from "../src/components/chat/tools/shared";

describe("readableOutput", () => {
  it("takes the text field of a JSON result", () => {
    expect(readableOutput('{"output": "hello\\n", "exit_code": 0}')).toBe("hello");
    expect(readableOutput('{"success": true, "content": "# Skill"}')).toBe("# Skill");
  });

  it("unwraps the sandbox's stdout carrying the handler's own JSON", () => {
    const inner = JSON.stringify({ output: "done", exit_code: 0 });
    expect(readableOutput(JSON.stringify({ stdout: inner }))).toBe("done");
  });

  it("joins an MCP result's text parts", () => {
    const result = { content: [{ type: "text", text: "a" }, { type: "image" }, { type: "text", text: "b" }] };
    expect(readableOutput(JSON.stringify(result))).toBe("a\nb");
  });

  it("pretty-prints JSON with no text field, and passes plain text through", () => {
    expect(readableOutput('{"results": [1]}')).toBe('{\n  "results": [\n    1\n  ]\n}');
    expect(readableOutput("  plain  ")).toBe("plain");
  });

  it("is empty for nothing to show", () => {
    expect(readableOutput(undefined)).toBe("");
    expect(readableOutput("  ")).toBe("");
    expect(readableOutput("null")).toBe("");
    expect(readableOutput('{"output": ""}')).toBe("");
  });
});

describe("toolFailure", () => {
  it("reads an identifier-like error as the code", () => {
    expect(toolFailure('{"error": "sandbox_unavailable", "message": "No sandbox"}')).toEqual({
      code: "sandbox_unavailable",
      message: "No sandbox",
    });
  });

  it("reads a prose error as the message", () => {
    expect(toolFailure('{"error": "Tool execution failed: timeout"}')).toEqual({
      code: null,
      message: "Tool execution failed: timeout",
    });
  });

  it("names a non-zero exit code and its output", () => {
    expect(toolFailure('{"exit_code": 2, "output": "no such file"}')).toEqual({
      code: "exit 2",
      message: "no such file",
    });
  });

  it("takes a result that is not JSON as the message", () => {
    expect(toolFailure("connection refused")).toEqual({ code: null, message: "connection refused" });
  });
});
