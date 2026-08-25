// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// The Simple-mode label vocabulary: pure functions that turn a tool
// call into the words a user reads.
//
// Simple mode draws a tool call as prose ("Edited landing.html",
// "Reading skill copywriting") rather than as the per-tool blocks the
// Expert view uses, so every tool needs an entry here. The vocabulary
// is deliberately complete — it covers tools that
// SIMPLE_MODE_HIDDEN_TOOLS currently filters out of the view too,
// because that policy changes (``skill_view`` moved across it) and an
// entry that was never exercised is an entry that was never right.

import { formatMcpToolLabel } from "../../lib/format";
import { parseArgs } from "./tools/shared";
import type { ToolCallInfo } from "../../types";

/**
 * A tool argument, only if the model actually sent a non-empty string.
 *
 * ``parseArgs`` is a ``JSON.parse`` behind an unchecked generic, so
 * every field can arrive as any type — or half-written, mid-stream.
 * Nothing here may throw: the SDK has no ErrorBoundary, so one bad row
 * unmounts the whole thread.
 */
function stringField(args: Record<string, unknown> | null, key: string): string | null {
  const value = args?.[key];
  return typeof value === "string" && value.length > 0 ? value : null;
}

export function cancelledToolLabel(toolName: string): string {
  const map: Record<string, string> = {
    terminal: "Command",
    execute_code: "Execute code",
    read_file: "Read",
    write_file: "Write",
    patch: "Patch",
    search_files: "Search files",
    list_files: "List files",
    web_search: "Web search",
    web_extract: "Web fetch",
    web_crawl: "Web crawl",
    session_search: "Session search",
    memory: "Memory",
    todo: "Todo",
    skills_list: "Skills",
    skill_view: "Skill",
    consult_expert: "Expert",
    delegate_task: "Delegate task",
    ask_user_question: "Ask User Question",
    process: "Process",
    create_artifact: "Create artifact",
    run_coding_agent: "Coding agent",
    code_run: "Coding agent",
    // ``Research memory`` reads as a noun ("memory of research") and
    // makes the shimmer awkward ("Running Research memory…").  The
    // one-liner renderer shows just ``Research`` as the label, so
    // mirror that here so the live shimmer matches the row that
    // replaces it on completion.
    research_memory: "Research",
    research_outline: "Research",
    generate_image: "image generation",
    generate_video: "video generation",
    // Arbor research-mission tools (the /auto-research coordinator loop).
    idea_tree: "Idea tree",
    dispatch_experiments: "Dispatch experiments",
    merge_experiment: "Merge experiment",

  };
  if (map[toolName]) return map[toolName];
  // MCP tools arrive as `mcp__{server}__{tool}`; show a clean label rather
  // than the raw prefixed name (matches the Expert-mode MCP renderer).
  if (toolName.startsWith("mcp__")) return formatMcpToolLabel(toolName);
  return toolName;
}

/**
 * Label for one or more ``skill_view`` calls — see
 * SIMPLE_MODE_HIDDEN_TOOLS for why skill loads get a deterministic row.
 *
 * Shared by the collapsed header, the expanded body row and the live
 * shimmer so the row never renames itself as the iteration completes.
 */
export function skillViewLabel(calls: ToolCallInfo[]): string {
  const args = calls.map((tc) => parseArgs<Record<string, unknown>>(tc.args));
  const names = args
    .map((a) => stringField(a, "name") ?? stringField(a, "skill"))
    .filter((name): name is string => name !== null);
  // Args stream in a character at a time, so a call can be mid-flight
  // with no parseable name yet; fall back to a countable phrasing
  // rather than rendering a half-written name. ``names.length === 0``
  // also keeps the function total: callers reach it through an
  // ``every`` test, which an empty list passes.
  if (names.length !== calls.length || names.length === 0) {
    return calls.length > 1
      ? `Reading ${calls.length} skills`
      : "Reading a skill";
  }
  if (calls.length === 1) {
    const path = stringField(args[0] ?? null, "file_path");
    return path
      ? `Reading skill ${names[0]} · ${lastPathSegment(path)}`
      : `Reading skill ${names[0]}`;
  }
  return `Reading skills ${names.join(", ")}`;
}

/**
 * Header label for a single-tool iteration. Most tools surface a
 * short detail (path basename, query, URL) so the header reads
 * "Read · landing.html". Tools whose detail is structurally noisy —
 * shell commands, arbitrary code blocks, raw memory keys — drop the
 * detail entirely and use a generic verb instead; the full detail
 * still appears in the expanded body via toolRowLabel.
 */
export function deriveSingleToolLabel(tc: ToolCallInfo): string {
  const name = cancelledToolLabel(tc.toolName);
  if (_HEADER_HIDES_DETAIL.has(tc.toolName)) {
    return _HEADER_GENERIC_VERB[tc.toolName] ?? name;
  }
  const detail = extractToolDetail(tc);
  return detail ? `${name} · ${detail}` : name;
}

const _HEADER_HIDES_DETAIL: ReadonlySet<string> = new Set([
  "terminal",
  "execute_code",
  "memory",
]);

const _HEADER_GENERIC_VERB: Record<string, string> = {
  terminal: "Ran a command",
  execute_code: "Executed code",
  memory: "Updated memory",
};

/**
 * Pull a short, human-readable detail string from a tool call's
 * arguments. Defensive against unparseable / partial JSON during
 * streaming.
 */
export function extractToolDetail(tc: ToolCallInfo): string | null {
  const args = parseArgs<Record<string, unknown>>(tc.args);
  if (!args) return null;
  // Prefer the most-meaningful arg per tool family.
  const stringArg = (key: string): string | null => stringField(args, key);
  switch (tc.toolName) {
    case "read_file":
    case "write_file":
    case "patch":
    case "list_files": {
      const path = stringArg("path") ?? stringArg("file_path");
      return path ? lastPathSegment(path) : null;
    }
    // ``pattern`` is the query; ``path`` is the optional root to search
    // under. Reading ``path`` here labelled the search *directory* as
    // the query, and dropped the label entirely for the common
    // whole-workspace search that omits it.
    case "search_files":
      return stringArg("pattern") ?? stringArg("query");
    case "terminal": {
      const cmd = stringArg("command");
      return cmd ? truncate(cmd, 40) : null;
    }
    case "execute_code": {
      const code = stringArg("code");
      return code ? truncate(code.split("\n")[0] ?? "", 40) : null;
    }
    case "web_search":
    case "web_crawl":
      return stringArg("query");
    case "web_extract": {
      const url = stringArg("url");
      if (!url) return null;
      // Hostname is much more readable in a one-line chip than the
      // full URL; falls back to the raw value for unparseable inputs
      // (file://, data:, etc.).
      try {
        return new URL(url).hostname.replace(/^www\./, "");
      } catch {
        return truncate(url, 40);
      }
    }
    case "skill_view":
    case "skill_manage":
      return stringArg("name") ?? stringArg("skill");
    case "create_artifact":
    case "consult_expert":
    case "delegate_task":
      return stringArg("name") ?? stringArg("task") ?? stringArg("title");
    case "run_coding_agent":
    case "code_run": {
      const prompt = stringArg("prompt");
      return prompt ? truncate(prompt, 60) : null;
    }
    case "idea_tree":
      // The action ("add", "view", "report", …) is the meaningful verb;
      // the header reads "Idea tree · report".
      return stringArg("action");
    case "dispatch_experiments": {
      const keys = args.node_keys;
      if (Array.isArray(keys) && keys.length > 0) {
        return truncate(keys.map((k) => String(k)).join(", "), 40);
      }
      return stringArg("action");
    }
    case "merge_experiment":
      return stringArg("node_key") ?? stringArg("action");
    case "memory":
      return stringArg("action") ?? stringArg("key");
    case "research_memory": {
      // ``add`` carries url+title; ``retrieve`` carries query; ``list``
      // carries nothing useful.  The Simple-mode row prefers a human
      // anchor over the raw action verb.
      const action = stringArg("action");
      if (action === "add") {
        const title = stringArg("title");
        if (title) return truncate(title, 60);
        const url = stringArg("url");
        if (url) {
          try {
            return new URL(url).hostname.replace(/^www\./, "");
          } catch {
            return truncate(url, 40);
          }
        }
        return null;
      }
      if (action === "retrieve") {
        return stringArg("query");
      }
      return action;
    }
    case "research_outline": {
      const action = stringArg("action");
      if (action === "set") {
        // Count level-2+ markdown headings in the outline so the row
        // surfaces "set outline (10 sections)" rather than the bare
        // tool name.  Mirrors ``outline_sections`` on the Python side.
        const outline = stringArg("outline");
        if (outline) {
          const sections = outline
            .split(/\r?\n/)
            .filter((line) => /^#{2,6}\s+\S/.test(line)).length;
          if (sections > 0) {
            return `${sections} ${sections === 1 ? "section" : "sections"}`;
          }
        }
        return "outline";
      }
      return action;
    }
    default:
      return null;
  }
}

function lastPathSegment(path: string): string {
  const cleaned = path.replace(/\/+$/, "");
  const idx = cleaned.lastIndexOf("/");
  return idx >= 0 ? cleaned.slice(idx + 1) : cleaned;
}

function truncate(s: string, max: number): string {
  return s.length > max ? `${s.slice(0, max - 1)}…` : s;
}

/**
 * Verb-first prose line for a tool call ("Edited landing.html",
 * "Read the frontend-design skill"). Falls back to the human tool
 * name when we can't extract a useful detail from the args.
 */
export function toolRowLabel(tc: ToolCallInfo): string {
  const detail = extractToolDetail(tc);
  switch (tc.toolName) {
    case "read_file":
      return detail ? `Read ${detail}` : "Read a file";
    case "write_file":
      return detail ? `Wrote ${detail}` : "Wrote a file";
    case "patch":
      return detail ? `Edited ${detail}` : "Edited a file";
    case "list_files":
      return detail ? `Listed ${detail}` : "Listed files";
    case "search_files": {
      if (!detail) return "Searched files";
      // ``target`` picks the axis: file *contents* (the default) or
      // file *names*. Naming the wrong one misreports what the agent
      // actually looked at. The server accepts "find"/"grep" as legacy
      // aliases and maps them before searching, so the raw argument the
      // model emitted can carry either spelling.
      const target = stringField(parseArgs(tc.args), "target");
      return target === "files" || target === "find"
        ? `Looked for files named "${detail}"`
        : `Searched files for "${detail}"`;
    }
    case "terminal":
      // Raw shell commands carry too much noise (paths, escapes,
      // chained pipes) to read well even as a body row. The Expert
      // view has the full block with output if the user needs it.
      return "Ran a command";
    case "execute_code":
      return "Executed code";
    case "web_search":
      return detail ? `Searched the web for "${detail}"` : "Searched the web";
    case "web_crawl":
      return detail ? `Crawled "${detail}"` : "Crawled the web";
    case "web_extract":
      return detail ? `Fetched ${detail}` : "Fetched a page";
    case "session_search":
      return detail ? `Searched session for "${detail}"` : "Searched session";
    case "skill_view":
      return skillViewLabel([tc]);
    case "skills_list":
      return "Listed available skills";
    case "skill_manage":
      return detail ? `Updated skill ${detail}` : "Managed skills";
    case "consult_expert":
      return detail ? `Consulted ${detail} expert` : "Consulted an expert";
    case "delegate_task":
      return detail ? `Delegated: ${detail}` : "Delegated a task";
    case "create_artifact":
      return detail ? `Created artifact "${detail}"` : "Created an artifact";
    case "memory":
      return detail ? `Memory ${detail}` : "Updated memory";
    case "todo":
      return detail ? `Todo ${detail}` : "Updated todo list";
    case "run_coding_agent":
    case "code_run": {
      const a = parseArgs<{ agent?: string; provider?: string }>(tc.args);
      const agent =
        a?.agent ??
        (a?.provider === "openai"
          ? "codex"
          : a?.provider === "anthropic"
            ? "claude"
            : undefined);
      const name =
        agent === "codex"
          ? "Codex"
          : agent === "claude"
            ? "Claude Code"
            : "coding agent";
      return detail ? `${name}: ${detail}` : `Ran ${name}`;
    }
    case "research_memory": {
      // ``detail`` already encodes the action's anchor (title /
      // hostname for add, query for retrieve, raw verb otherwise);
      // wrap it in the verb-first prose the rest of the row family
      // uses.
      const args = parseArgs<{ action?: string }>(tc.args);
      const action = args?.action;
      if (action === "add") {
        return detail ? `Stored source "${detail}"` : "Stored a source";
      }
      if (action === "retrieve") {
        return detail
          ? `Retrieved sources for "${detail}"`
          : "Retrieved sources";
      }
      if (action === "list") {
        return "Listed sources";
      }
      return "Updated research memory";
    }
    case "research_outline": {
      const args = parseArgs<{ action?: string }>(tc.args);
      const action = args?.action;
      if (action === "set") {
        return detail ? `Updated outline (${detail})` : "Updated outline";
      }
      if (action === "get") {
        return "Read outline";
      }
      return "Touched outline";
    }
    default:
      return cancelledToolLabel(tc.toolName);
  }
}
