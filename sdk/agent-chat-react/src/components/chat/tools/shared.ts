// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Shared utilities for tool call renderers.

/**
 * Map a tool-call status to a Tailwind background-color class for use
 * as a TimelineIndicator color.
 */
export function statusColorClass(status: ToolStatus): string {
  if (status === "running") return "bg-primary animate-pulse";
  if (status === "error") return "bg-red-500";
  if (status === "cancelled") return "bg-muted-foreground/40";
  return "bg-emerald-500";
}

export type ToolStatus = "running" | "complete" | "error" | "cancelled";

/**
 * Derive the effective status from a tool call.  Cancelled siblings
 * (parallel-batch cancellations) are distinguished from genuine errors
 * so the timeline dot reads as muted rather than red.
 */
export function effectiveStatus(tc: {
  status: "running" | "complete" | "error";
  result?: string;
  cancelled?: boolean;
}): ToolStatus {
  if (tc.cancelled) return "cancelled";
  if (tc.status !== "complete" || !tc.result) return tc.status;
  try {
    const parsed = JSON.parse(tc.result);
    if (parsed?.exit_code !== undefined && parsed.exit_code !== 0) return "error";
    if (parsed?.error) return "error";
    if (parsed?.status === "blocked" || parsed?.status === "error") return "error";
    if (parsed?.success === false) return "error";
  } catch { /* ignore */ }
  return "complete";
}

export function formatArgs(args: string): string {
  try {
    return JSON.stringify(JSON.parse(args), null, 2);
  } catch {
    return args;
  }
}

export function parseArgs<T = Record<string, unknown>>(args: string): T | null {
  try {
    return JSON.parse(args) as T;
  } catch {
    return null;
  }
}

export function truncate(s: string, max: number): string {
  return s.length > max ? s.slice(0, max) + "\n... (truncated)" : s;
}

// The fields a tool's JSON result keeps its text in, by convention.
const OUTPUT_KEYS = ["output", "stdout", "content", "text", "result"] as const;

/**
 * A tool result as text a reader can scan: the text field of a JSON result
 * (one level of double-wrapping unwrapped, as the sandbox's stdout carries
 * the handler's own JSON), an MCP result's text parts, otherwise the JSON
 * pretty-printed, or the raw string when it is not JSON. "" when empty.
 */
export function readableOutput(result: string | undefined): string {
  if (!result?.trim()) return "";
  let value: unknown;
  try {
    value = JSON.parse(result);
  } catch {
    return result.trim();
  }
  for (let depth = 0; depth < 2; depth++) {
    if (value === null || value === undefined) return "";
    if (typeof value !== "object") return String(value).trim();
    const obj = value as Record<string, unknown>;
    const text = OUTPUT_KEYS.map((key) => obj[key]).find((v) => typeof v === "string");
    if (typeof text === "string") {
      try {
        value = JSON.parse(text);
        continue;
      } catch {
        return text.trim();
      }
    }
    if (Array.isArray(obj.content)) {
      const parts = obj.content
        .map((part) => (typeof part?.text === "string" ? part.text : ""))
        .filter(Boolean);
      if (parts.length > 0) return parts.join("\n").trim();
    }
    break;
  }
  return JSON.stringify(value, null, 2);
}

/**
 * A failed tool's error code and message, as far as its result says them.
 * `error` is the code when it reads as one (`sandbox_unavailable`), and the
 * message when it is prose (`Tool execution failed: …`).
 */
export function toolFailure(result: string | undefined): {
  code: string | null;
  message: string;
} {
  let parsed: Record<string, unknown> | null = null;
  try {
    const value = JSON.parse(result ?? "");
    if (value && typeof value === "object") parsed = value as Record<string, unknown>;
  } catch { /* not JSON: the result is the message */ }
  if (!parsed) return { code: null, message: (result ?? "").trim() };
  const error = typeof parsed.error === "string" ? parsed.error : "";
  const errorIsCode = /^[\w.:-]+$/.test(error);
  const code =
    typeof parsed.code === "string" || typeof parsed.code === "number"
      ? String(parsed.code)
      : typeof parsed.exit_code === "number" && parsed.exit_code !== 0
        ? `exit ${parsed.exit_code}`
        : errorIsCode
          ? error
          : null;
  const message = [
    parsed.message,
    parsed.reason,
    parsed.detail,
    errorIsCode ? undefined : error,
    parsed.stderr,
    parsed.output,
  ].find((v): v is string => typeof v === "string" && v.trim() !== "");
  return { code, message: message?.trim() ?? "" };
}

export function toolErrorSummary(result: string | undefined, max = 180): string {
  if (!result) return "";
  try {
    const parsed = JSON.parse(result);
    const error = typeof parsed?.error === "string" ? parsed.error : "";
    if (error === "sandbox_unavailable") {
      return "Sandbox is unavailable. Workspace commands cannot run right now.";
    }
    const reason = typeof parsed?.reason === "string" ? parsed.reason : "";
    const message = typeof parsed?.message === "string" ? parsed.message : "";
    const detail = reason || message;
    const summary = error && detail && detail !== error
      ? `${error}: ${detail}`
      : error || detail;
    return summary ? summary.replace(/\s+/g, " ").slice(0, max) : "";
  } catch {
    return "";
  }
}
