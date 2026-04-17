// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Shared utilities for tool call renderers.

/**
 * Map a tool-call status to a Tailwind background-color class for use
 * as a TimelineIndicator color.
 */
export function statusColorClass(status: "running" | "complete" | "error"): string {
  if (status === "running") return "bg-primary animate-pulse";
  if (status === "error") return "bg-red-500";
  return "bg-emerald-500";
}

/**
 * Derive the effective status from a tool call by inspecting the result
 * for error indicators (non-zero exit code, error field, blocked status).
 */
export function effectiveStatus(tc: { status: "running" | "complete" | "error"; result?: string }): "running" | "complete" | "error" {
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
