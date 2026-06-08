// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//

/**
 * Format a byte count as a human-readable size string (B / KB / MB).
 */
export function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/**
 * Turn an MCP tool name into a human-readable label.
 *
 * MCP tools are emitted as ``mcp__{server}__{tool}`` (the server segment
 * itself may contain ``__``; the tool is always the final segment). We drop
 * the ``mcp__`` prefix and the server segments, then title-case the tool:
 * ``mcp__composio_tool_router__COMPOSIO_SEARCH_TOOLS`` → ``Composio Search Tools``.
 *
 * Used by both Simple and Expert chat views so a tool reads the same in
 * either mode rather than leaking the raw prefixed name.
 */
export function formatMcpToolLabel(toolName: string): string {
  const segments = toolName.replace(/^mcp__/, "").split("__");
  const tool = segments[segments.length - 1] ?? toolName;
  return tool
    .split("_")
    .filter(Boolean)
    .map((p) => p.charAt(0).toUpperCase() + p.slice(1).toLowerCase())
    .join(" ");
}

/**
 * Infer a language hint from a file path for syntax display.
 *
 * Extracts the extension from the filename component only, so directory
 * names containing dots (e.g. ``some.dir/Makefile``) are handled correctly.
 */
export function getLanguageHint(path: string): string {
  const name = path.split("/").pop() ?? path;
  const dot = name.lastIndexOf(".");
  if (dot < 0) return "plaintext";
  const ext = name.slice(dot).toLowerCase();
  const map: Record<string, string> = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".jsx": "jsx",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".sql": "sql",
    ".sh": "bash",
    ".css": "css",
    ".html": "html",
    ".md": "markdown",
    ".rs": "rust",
    ".go": "go",
    ".java": "java",
    ".rb": "ruby",
    ".php": "php",
    ".c": "c",
    ".cpp": "cpp",
    ".h": "c",
    ".hpp": "cpp",
  };
  return map[ext] ?? "plaintext";
}
