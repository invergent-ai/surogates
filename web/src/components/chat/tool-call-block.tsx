// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import { useEffect, useMemo, useRef, useState } from "react";
import {
  CheckCircle2Icon,
  ChevronRightIcon,
  Loader2Icon,
  AlertCircleIcon,
  ListTodoIcon,
} from "lucide-react";
import {
  Queue,
  QueueItem,
  QueueItemContent,
  QueueItemDescription,
  QueueItemIndicator,
  QueueList,
  QueueSection,
  QueueSectionContent,
  QueueSectionLabel,
  QueueSectionTrigger,
} from "@/components/ai-elements/queue";
import {
  Terminal,
  TerminalActions,
  TerminalCopyButton,
  TerminalHeader,
  TerminalTitle,
} from "@/components/ai-elements/terminal";
import { cn } from "@/lib/utils";
import type { ToolCallInfo } from "@/hooks/use-session-runtime";

const STATUS_ICON = {
  running: <Loader2Icon className="size-3.5 animate-spin text-primary" />,
  complete: <CheckCircle2Icon className="size-3.5 text-emerald-500" />,
  error: <AlertCircleIcon className="size-3.5 text-destructive" />,
} as const;

// ── Terminal result detection ───────────────────────────────────────

interface TerminalResult {
  output: string;
  exit_code: number;
  error: string | null;
  command: string;
}

function parseTerminalResult(
  result: string | undefined,
  args: string,
): TerminalResult | null {
  if (!result) return null;
  try {
    const parsed = JSON.parse(result);
    if (typeof parsed?.output !== "string" && typeof parsed?.exit_code !== "number") {
      return null;
    }
    let command = "";
    try {
      const parsedArgs = JSON.parse(args);
      command = parsedArgs?.command ?? "";
    } catch { /* ignore */ }
    return {
      output: parsed.output ?? "",
      exit_code: parsed.exit_code ?? 0,
      error: parsed.error ?? null,
      command,
    };
  } catch {
    return null;
  }
}

// ── Terminal result renderer ────────────────────────────────────────

const COLLAPSED_HEIGHT = 96; // px — ~6 lines of mono text

function TerminalToolResult({ result, isRunning }: { result: TerminalResult; isRunning: boolean }) {
  const exitOk = result.exit_code === 0;
  const output = result.output || result.error || "";
  const [expanded, setExpanded] = useState(false);
  const contentRef = useRef<HTMLDivElement>(null);
  const [overflows, setOverflows] = useState(false);

  useEffect(() => {
    if (contentRef.current) {
      setOverflows(contentRef.current.scrollHeight > COLLAPSED_HEIGHT);
    }
  }, [output]);

  // Build IN/OUT content for Claude Code-style display.
  const terminalContent = [
    result.command && `${result.command}`,
    output ? `\n${output}` : (!isRunning ? "\n(no output)" : ""),
  ].filter(Boolean).join("");

  return (
    <Terminal
      output={terminalContent}
      isStreaming={isRunning}
      className="my-1 w-full text-xs"
    >
      <TerminalHeader className="py-1.5 px-3">
        <div className="flex items-center gap-2 min-w-0 text-xs">
          <span className="font-semibold text-zinc-100 shrink-0">Bash</span>
          {!isRunning && (
            <span className={cn(
              "text-[10px] font-mono shrink-0",
              exitOk ? "text-emerald-500" : "text-red-400",
            )}>
              [{result.exit_code}]
            </span>
          )}
        </div>
        <TerminalActions>
          <TerminalCopyButton className="size-6" />
        </TerminalActions>
      </TerminalHeader>
      <div className="relative">
        <div
          ref={contentRef}
          className={cn(
            "overflow-hidden px-3 py-2 font-mono text-xs leading-relaxed transition-[max-height] duration-200",
            !expanded && "max-h-24",
          )}
          style={expanded ? { maxHeight: contentRef.current?.scrollHeight } : undefined}
        >
          {result.command && (
            <div className="flex gap-2 text-zinc-400">
              <span className="shrink-0 select-none text-emerald-600">IN</span>
              <pre className="whitespace-pre-wrap wrap-break-word text-zinc-200">{result.command}</pre>
            </div>
          )}
          {(output || !isRunning) && (
            <div className="mt-1.5 flex gap-2 text-zinc-400">
              <span className="shrink-0 select-none text-sky-600">OUT</span>
              <pre className="whitespace-pre-wrap wrap-break-word">
                {output || <span className="text-zinc-500">(no output)</span>}
              </pre>
            </div>
          )}
          {isRunning && !output && (
            <span className="ml-0.5 inline-block h-3.5 w-1.5 animate-pulse bg-zinc-100" />
          )}
        </div>
        {overflows && !expanded && (
          <div className="absolute inset-x-0 bottom-0 flex items-end justify-center bg-linear-to-t from-zinc-950 to-transparent pt-6 pb-1">
            <button
              type="button"
              onClick={() => setExpanded(true)}
              className="text-[11px] text-zinc-400 hover:text-zinc-200 transition-colors font-medium"
            >
              Show more
            </button>
          </div>
        )}
        {overflows && expanded && (
          <div className="flex justify-center pb-1">
            <button
              type="button"
              onClick={() => setExpanded(false)}
              className="text-[11px] text-zinc-400 hover:text-zinc-200 transition-colors font-medium"
            >
              Show less
            </button>
          </div>
        )}
      </div>
    </Terminal>
  );
}

// ── Todo result detection ───────────────────────────────────────────

interface TodoItem {
  id: string;
  content: string;
  status: "pending" | "in_progress" | "completed";
  description?: string;
}

function parseTodoResult(result: string | undefined): TodoItem[] | null {
  if (!result) return null;
  try {
    const parsed = JSON.parse(result);
    const todos = parsed?.todos ?? parsed;
    if (!Array.isArray(todos)) return null;
    if (todos.length === 0) return null;
    if (!todos.every((t: unknown) =>
      typeof t === "object" && t !== null &&
      "id" in t && "content" in t && "status" in t
    )) return null;
    return todos as TodoItem[];
  } catch {
    return null;
  }
}

// ── Todo result renderer ────────────────────────────────────────────

function TodoResult({ todos }: { todos: TodoItem[] }) {
  const completed = todos.filter((t) => t.status === "completed").length;
  const total = todos.length;

  return (
    <Queue className="mt-1">
      <QueueSection>
        <QueueSectionTrigger>
          <QueueSectionLabel
            count={total}
            label={`tasks (${completed}/${total} done)`}
            icon={<ListTodoIcon className="size-3.5" />}
          />
        </QueueSectionTrigger>
        <QueueSectionContent>
          <QueueList>
            {todos.map((todo) => {
              const isCompleted = todo.status === "completed";
              const isInProgress = todo.status === "in_progress";
              return (
                <QueueItem key={todo.id}>
                  <div className="flex items-center gap-2">
                    {isInProgress ? (
                      <Loader2Icon className="size-2.5 animate-spin text-primary shrink-0" />
                    ) : (
                      <QueueItemIndicator completed={isCompleted} />
                    )}
                    <QueueItemContent completed={isCompleted}>
                      {todo.content}
                    </QueueItemContent>
                  </div>
                  {todo.description && (
                    <QueueItemDescription completed={isCompleted}>
                      {todo.description}
                    </QueueItemDescription>
                  )}
                </QueueItem>
              );
            })}
          </QueueList>
        </QueueSectionContent>
      </QueueSection>
    </Queue>
  );
}

// ── Main tool call block ────────────────────────────────────────────

export function ToolCallBlock({ tc }: { tc: ToolCallInfo }) {
  const [expanded, setExpanded] = useState(false);

  const todos = useMemo(() =>
    tc.toolName === "todo" ? parseTodoResult(tc.result) : null,
  [tc.toolName, tc.result]);

  const terminalResult = useMemo(() =>
    tc.toolName === "terminal" ? parseTerminalResult(tc.result, tc.args) : null,
  [tc.toolName, tc.result, tc.args]);

  // Todo tool — render inline Queue.
  if (tc.toolName === "todo" && todos) {
    return <TodoResult todos={todos} />;
  }

  // Terminal tool — render rich terminal output.
  if (tc.toolName === "terminal") {
    const isRunning = tc.status === "running";
    if (terminalResult) {
      return <TerminalToolResult result={terminalResult} isRunning={isRunning} />;
    }
    // Running with no result yet — show command with spinner.
    if (isRunning) {
      let command = "";
      try { command = JSON.parse(tc.args)?.command ?? ""; } catch { /* ignore */ }
      return (
        <Terminal output="" isStreaming className="my-1 w-full text-xs">
          <TerminalHeader className="py-1.5 px-3">
            <div className="flex items-center gap-2 min-w-0 text-xs">
              <span className="font-semibold text-zinc-100 shrink-0">Bash</span>
            </div>
          </TerminalHeader>
        </Terminal>
      );
    }
  }

  // Default — generic collapsible tool call.
  return (
    <div className="my-1">
      <button
        type="button"
        onClick={() => setExpanded(!expanded)}
        className={cn(
          "flex w-full items-center gap-1.5 rounded-md px-2 py-1",
          "text-xs text-muted-foreground hover:bg-muted/50 transition-colors",
          "font-mono",
        )}
      >
        <ChevronRightIcon
          className={cn(
            "size-3 shrink-0 transition-transform duration-150",
            expanded && "rotate-90",
          )}
        />
        {STATUS_ICON[tc.status]}
        <span className="font-medium text-foreground/80">{tc.toolName}</span>
      </button>

      {expanded && (
        <div className="ml-6 mt-0.5 space-y-1 text-xs font-mono">
          <pre className="overflow-x-auto rounded bg-muted/40 px-2 py-1 text-muted-foreground whitespace-pre-wrap break-all">
            {formatArgs(tc.args)}
          </pre>
          {tc.result && (
            <pre className="overflow-x-auto rounded bg-muted/40 px-2 py-1 text-muted-foreground whitespace-pre-wrap break-all max-h-64 overflow-y-auto">
              <span className="text-emerald-600 dark:text-emerald-400">
                Result:
              </span>
              {"\n"}
              {truncate(tc.result, 2000)}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}

function formatArgs(args: string): string {
  try {
    return JSON.stringify(JSON.parse(args), null, 2);
  } catch {
    return args;
  }
}

function truncate(s: string, max: number): string {
  return s.length > max ? s.slice(0, max) + "\n... (truncated)" : s;
}
