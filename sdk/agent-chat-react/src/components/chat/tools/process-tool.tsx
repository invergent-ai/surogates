// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Renderer for the "process" tool — manages background processes started
// via terminal(background=true).  Handles all 7 actions: list, poll, log,
// wait, kill, write, submit.

import { useEffect, useRef, useState } from "react";
import {
  ActivityIcon,
  CircleCheckIcon,
  CircleDotIcon,
  CircleXIcon,
  ClockIcon,
  Loader2Icon,
  SkullIcon,
} from "lucide-react";
import {
  Terminal,
  TerminalHeader,
} from "../../ai-elements/terminal";
import { Button } from "../../ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "../../ui/dialog";
import { ScrollArea } from "../../ui/scroll-area";
import { cn } from "../../../lib/utils";
import type { ToolCallInfo } from "../../../types";

// ── Types ───────────────────────────────────────────────────────────

interface ProcessEntry {
  session_id: string;
  command: string;
  cwd?: string;
  pid?: number;
  started_at?: string;
  uptime_seconds?: number;
  status: "running" | "exited";
  output_preview?: string;
  exit_code?: number | null;
  detached?: boolean;
}

interface ProcessArgs {
  action: string;
  session_id?: string;
  data?: string;
  timeout?: number;
  offset?: number;
  limit?: number;
}

// ── Helpers ─────────────────────────────────────────────────────────

function parseArgs(args: string): ProcessArgs {
  try {
    return JSON.parse(args) as ProcessArgs;
  } catch {
    return { action: "unknown" };
  }
}

function parseResult(result: string | undefined): Record<string, unknown> | null {
  if (!result) return null;
  try {
    return JSON.parse(result) as Record<string, unknown>;
  } catch {
    return null;
  }
}

function formatUptime(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  return `${h}h ${m}m`;
}

function truncateCommand(cmd: string, max = 80): string {
  return cmd.length > max ? cmd.slice(0, max) + "..." : cmd;
}

// ── Status badge ────────────────────────────────────────────────────

function StatusBadge({ status, exitCode }: { status: string; exitCode?: number | null }) {
  if (status === "running") {
    return (
      <span className="inline-flex items-center gap-1 text-xs font-medium text-emerald-600 dark:text-emerald-400">
        <CircleDotIcon className="size-3 animate-pulse" />
        running
      </span>
    );
  }
  if (status === "exited") {
    const ok = exitCode === 0;
    return (
      <span className={cn(
        "inline-flex items-center gap-1 text-xs font-medium",
        ok ? "text-muted-foreground" : "text-red-500 dark:text-red-400",
      )}>
        {ok
          ? <CircleCheckIcon className="size-3" />
          : <CircleXIcon className="size-3" />}
        exited({exitCode ?? "?"})
      </span>
    );
  }
  if (status === "killed") {
    return (
      <span className="inline-flex items-center gap-1 text-xs font-medium text-orange-500 dark:text-orange-400">
        <SkullIcon className="size-3" />
        killed
      </span>
    );
  }
  if (status === "timeout") {
    return (
      <span className="inline-flex items-center gap-1 text-xs font-medium text-amber-500 dark:text-amber-400">
        <ClockIcon className="size-3" />
        timeout
      </span>
    );
  }
  if (status === "interrupted") {
    return (
      <span className="inline-flex items-center gap-1 text-xs font-medium text-amber-500 dark:text-amber-400">
        <CircleXIcon className="size-3" />
        interrupted
      </span>
    );
  }
  // not_found, error, ok, already_exited, etc.
  return (
    <span className="inline-flex items-center gap-1 text-xs font-medium text-muted-foreground">
      {status}
    </span>
  );
}

// ── Action: list ────────────────────────────────────────────────────

function ProcessListView({ processes }: { processes: ProcessEntry[] }) {
  if (processes.length === 0) {
    return (
      <div className="flex items-center gap-2 px-3 py-2 text-sm text-muted-foreground ">
        <ActivityIcon className="size-3.5 shrink-0" />
        No background processes
      </div>
    );
  }

  return (
    <div className="space-y-px">
      {processes.map((p) => (
        <div
          key={p.session_id}
          className="flex items-start gap-3 px-3 py-1.5  text-sm"
        >
          <div className="flex flex-col gap-0.5 min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <StatusBadge status={p.status} exitCode={p.exit_code} />
              <span className="text-muted-foreground text-xs shrink-0">
                {p.session_id}
              </span>
              {p.pid != null && (
                <span className="text-muted-foreground/60 text-xs shrink-0">
                  pid:{p.pid}
                </span>
              )}
              {p.uptime_seconds != null && (
                <span className="text-muted-foreground/60 text-xs shrink-0">
                  {formatUptime(p.uptime_seconds)}
                </span>
              )}
              {p.detached && (
                <span className="text-xs text-amber-500/80">detached</span>
              )}
            </div>
            <pre className="text-foreground/80 whitespace-pre-wrap wrap-break-word text-xs">
              {truncateCommand(p.command, 120)}
            </pre>
          </div>
        </div>
      ))}
    </div>
  );
}

// ── Action: poll ────────────────────────────────────────────────────

function ProcessPollView({ data }: { data: Record<string, unknown> }) {
  const status = (data.status as string) ?? "unknown";
  const command = (data.command as string) ?? "";
  const sessionId = (data.session_id as string) ?? "";
  const pid = data.pid as number | undefined;
  const uptime = data.uptime_seconds as number | undefined;
  const exitCode = data.exit_code as number | null | undefined;
  const preview = (data.output_preview as string) ?? "";

  return (
    <div className="space-y-1">
      <div className="flex items-center gap-2 px-3 pt-1.5  text-sm">
        <StatusBadge status={status} exitCode={exitCode} />
        <span className="text-muted-foreground text-xs">{sessionId}</span>
        {pid != null && (
          <span className="text-muted-foreground/60 text-xs">pid:{pid}</span>
        )}
        {uptime != null && (
          <span className="text-muted-foreground/60 text-xs">{formatUptime(uptime)}</span>
        )}
      </div>
      {command && (
        <pre className="px-3  text-xs text-foreground/80 whitespace-pre-wrap wrap-break-word">
          {truncateCommand(command, 120)}
        </pre>
      )}
      {preview && (
        <OutputPreview output={preview} />
      )}
    </div>
  );
}

// ── Action: log ─────────────────────────────────────────────────────

const LOG_COLLAPSED_HEIGHT = 96;

function ProcessLogView({ data }: { data: Record<string, unknown> }) {
  const output = (data.output as string) ?? "";
  const status = (data.status as string) ?? "";
  const totalLines = data.total_lines as number | undefined;
  const showing = (data.showing as string) ?? "";
  const sessionId = (data.session_id as string) ?? "";

  const [dialogOpen, setDialogOpen] = useState(false);
  const contentRef = useRef<HTMLDivElement>(null);
  const [overflows, setOverflows] = useState(false);

  useEffect(() => {
    if (contentRef.current) {
      setOverflows(contentRef.current.scrollHeight > LOG_COLLAPSED_HEIGHT);
    }
  }, [output]);

  return (
    <>
      <div className="space-y-1">
        <div className="flex items-center gap-2 px-3 pt-1.5  text-sm">
          <StatusBadge status={status} />
          <span className="text-muted-foreground text-xs">{sessionId}</span>
          {totalLines != null && (
            <span className="text-muted-foreground/60 text-xs">
              {showing} of {totalLines}
            </span>
          )}
        </div>
        <div
          ref={contentRef}
          role="button"
          tabIndex={0}
          onClick={() => overflows && setDialogOpen(true)}
          onKeyDown={(e) => { if (e.key === "Enter" && overflows) setDialogOpen(true); }}
          className={cn(
            "overflow-hidden px-3 py-1.5  text-xs leading-relaxed",
            overflows && "cursor-pointer max-h-24",
          )}
        >
          <pre className="whitespace-pre-wrap wrap-break-word text-foreground/80">
            {output || <span className="text-muted-foreground/60">(empty)</span>}
          </pre>
        </div>
        {overflows && (
          <div className="px-3 pb-1">
            <Button
              variant="outline"
              size="xs"
              onClick={() => setDialogOpen(true)}
            >
              Expand log
            </Button>
          </div>
        )}
      </div>

      <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
        <DialogContent className="sm:max-w-[50vw] w-full h-[70vh] flex flex-col p-0 gap-0 overflow-hidden">
          <DialogHeader className="px-4 py-3 border-b border-border shrink-0">
            <DialogTitle className=" text-sm">
              Process log — {sessionId}
            </DialogTitle>
          </DialogHeader>
          <ScrollArea className="flex-1 min-h-0">
            <pre className="px-4 py-3  text-sm leading-relaxed whitespace-pre-wrap wrap-break-word">
              {output || "(empty)"}
            </pre>
          </ScrollArea>
        </DialogContent>
      </Dialog>
    </>
  );
}

// ── Action: wait ────────────────────────────────────────────────────

function ProcessWaitView({ data }: { data: Record<string, unknown> }) {
  const status = (data.status as string) ?? "unknown";
  const exitCode = data.exit_code as number | null | undefined;
  const output = (data.output as string) ?? "";
  const note = (data.timeout_note as string) ?? (data.note as string) ?? "";

  return (
    <div className="space-y-1">
      <div className="flex items-center gap-2 px-3 pt-1.5  text-sm">
        <StatusBadge status={status} exitCode={exitCode} />
        {note && (
          <span className="text-muted-foreground/60 text-xs">{note}</span>
        )}
      </div>
      {output && <OutputPreview output={output} />}
    </div>
  );
}

// ── Action: kill ────────────────────────────────────────────────────

function ProcessKillView({ data }: { data: Record<string, unknown> }) {
  const status = (data.status as string) ?? "unknown";
  const sessionId = (data.session_id as string) ?? "";
  const exitCode = data.exit_code as number | null | undefined;
  const error = (data.error as string) ?? "";

  return (
    <div className="flex items-center gap-2 px-3 py-1.5  text-sm">
      <StatusBadge status={status} exitCode={exitCode} />
      {sessionId && (
        <span className="text-muted-foreground text-xs">{sessionId}</span>
      )}
      {error && (
        <span className="text-red-500 text-xs">{error}</span>
      )}
    </div>
  );
}

// ── Action: write / submit ──────────────────────────────────────────

function ProcessStdinView({ action, data }: { action: string; data: Record<string, unknown> }) {
  const status = (data.status as string) ?? "unknown";
  const bytesWritten = data.bytes_written as number | undefined;
  const error = (data.error as string) ?? "";

  const ok = status === "ok";
  return (
    <div className="flex items-center gap-2 px-3 py-1.5  text-sm">
      <span className={cn(
        "text-xs font-medium",
        ok ? "text-emerald-600 dark:text-emerald-400" : "text-red-500 dark:text-red-400",
      )}>
        {action === "submit" ? "stdin submit" : "stdin write"}
      </span>
      {ok
        ? <span className="text-muted-foreground/60 text-xs">{bytesWritten} bytes</span>
        : <span className="text-red-500 text-xs">{error || status}</span>}
    </div>
  );
}

// ── Shared output preview ───────────────────────────────────────────

function OutputPreview({ output }: { output: string }) {
  return (
    <div className="px-3 py-1.5  text-xs leading-relaxed max-h-20 overflow-hidden">
      <div className="flex gap-2 text-muted-foreground">
        <span className="shrink-0 select-none text-sky-600">OUT</span>
        <pre className="whitespace-pre-wrap wrap-break-word text-foreground/70">
          {output}
        </pre>
      </div>
    </div>
  );
}

// ── Main renderer ───────────────────────────────────────────────────

export function ProcessToolBlock({ tc }: { tc: ToolCallInfo }) {
  const isRunning = tc.status === "running";
  const args = parseArgs(tc.args);
  const result = parseResult(tc.result);
  const action = args.action || "unknown";

  const actionLabel: Record<string, string> = {
    list: "Process List",
    poll: "Process Poll",
    log: "Process Log",
    wait: "Process Wait",
    kill: "Process Kill",
    write: "Process Stdin",
    submit: "Process Stdin",
  };

  // Running state — show action header with spinner
  if (isRunning && !result) {
    return (
      <Terminal output="" isStreaming className="w-full text-sm">
        <TerminalHeader>
          <div className="flex items-center gap-1.5 min-w-0 text-sm ">
            <ActivityIcon className="size-3.5 shrink-0" />
            <span className="font-semibold text-foreground shrink-0">
              {actionLabel[action] ?? "Process"}
            </span>
            {args.session_id && (
              <span className="text-muted-foreground text-xs truncate">
                {args.session_id}
              </span>
            )}
          </div>
        </TerminalHeader>
        <div className="px-3 py-2 bg-background  text-sm flex items-center gap-2 text-muted-foreground">
          <Loader2Icon className="size-3 animate-spin" />
          <span className="text-xs">
            {action === "wait" ? `Waiting${args.timeout ? ` (${args.timeout}s timeout)` : ""}...` : "(no output)"}
          </span>
        </div>
      </Terminal>
    );
  }

  if (!result) return null;

  // Error result (not_found, generic error)
  const resultStatus = result.status as string | undefined;
  const resultError = result.error as string | undefined;
  if (resultStatus === "not_found" || (resultStatus === "error" && !["list", "poll", "log"].includes(action))) {
    return (
      <Terminal output="" className="w-full text-sm">
        <TerminalHeader>
          <div className="flex items-center gap-1.5 min-w-0 text-sm ">
            <ActivityIcon className="size-3.5 shrink-0" />
            <span className="font-semibold text-foreground shrink-0">
              {actionLabel[action] ?? "Process"}
            </span>
          </div>
        </TerminalHeader>
        <div className="px-3 py-2 bg-background  text-xs text-red-500 dark:text-red-400">
          {resultError || resultStatus}
        </div>
      </Terminal>
    );
  }

  // Dispatch to action-specific view
  let content: React.ReactNode;
  switch (action) {
    case "list":
      content = <ProcessListView processes={(result.processes as ProcessEntry[]) ?? []} />;
      break;
    case "poll":
      content = <ProcessPollView data={result} />;
      break;
    case "log":
      content = <ProcessLogView data={result} />;
      break;
    case "wait":
      content = <ProcessWaitView data={result} />;
      break;
    case "kill":
      content = <ProcessKillView data={result} />;
      break;
    case "write":
    case "submit":
      content = <ProcessStdinView action={action} data={result} />;
      break;
    default:
      content = (
        <pre className="px-3 py-2  text-xs text-muted-foreground whitespace-pre-wrap">
          {JSON.stringify(result, null, 2)}
        </pre>
      );
  }

  return (
    <Terminal output="" className="w-full text-sm">
      <TerminalHeader>
        <div className="flex items-center gap-1.5 min-w-0 text-sm ">
          <ActivityIcon className="size-3.5 shrink-0" />
          <span className="font-semibold text-foreground shrink-0">
            {actionLabel[action] ?? "Process"}
          </span>
          {args.session_id && !["list"].includes(action) && (
            <span className="text-muted-foreground text-xs truncate">
              {args.session_id}
            </span>
          )}
        </div>
      </TerminalHeader>
      <div className="bg-background pb-1">
        {content}
      </div>
    </Terminal>
  );
}