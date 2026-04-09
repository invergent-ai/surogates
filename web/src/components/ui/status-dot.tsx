// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import { cn } from "@/utils/cn";

type Status =
  | "running"
  | "serving"
  | "completed"
  | "deploying"
  | "error"
  | "stopped";

const STATUS_COLORS: Record<Status, string> = {
  running: "#22C55E",
  serving: "#22C55E",
  completed: "#3B82F6",
  deploying: "#F59E0B",
  error: "#EF4444",
  stopped: "#6B7280",
};

const GLOW_STATUSES = new Set<Status>(["running", "serving"]);

interface StatusDotProps {
  status: Status;
  className?: string;
}

export function StatusDot({ status, className }: StatusDotProps) {
  const color = STATUS_COLORS[status] ?? "#6B7280";

  return (
    <span
      className={cn("inline-block w-[7px] h-[7px] rounded-full shrink-0 mr-1.5", className)}
      style={{
        backgroundColor: color,
        boxShadow: GLOW_STATUSES.has(status) ? `0 0 6px ${color}` : "none",
      }}
    />
  );
}

export type { Status };
