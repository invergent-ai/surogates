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
