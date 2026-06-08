import { describe, expect, it } from "vitest";
import { formatMcpToolLabel } from "../src/lib/format";

describe("formatMcpToolLabel", () => {
  it("strips the mcp prefix, the server, and the Composio brand", () => {
    expect(
      formatMcpToolLabel(
        "mcp__composio_tool_router__COMPOSIO_SEARCH_TOOLS",
      ),
    ).toBe("Search Tools");
  });

  it("drops the COMPOSIO_ prefix from every meta-tool", () => {
    expect(
      formatMcpToolLabel("mcp__composio_tool_router__COMPOSIO_MANAGE_CONNECTIONS"),
    ).toBe("Manage Connections");
  });

  it("keeps only the final segment when the server name contains __", () => {
    expect(
      formatMcpToolLabel("mcp__org_user_agent__composio_tool_router__GMAIL_SEND_EMAIL"),
    ).toBe("Gmail Send Email");
  });

  it("handles a name with no server segment", () => {
    expect(formatMcpToolLabel("mcp__LIST_FILES")).toBe("List Files");
  });

  it("title-cases a bare tool name without the mcp prefix", () => {
    expect(formatMcpToolLabel("SEARCH_TOOLS")).toBe("Search Tools");
  });
});
