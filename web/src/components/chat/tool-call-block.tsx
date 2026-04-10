// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import { useEffect, useMemo, useRef, useState } from "react";
import {
  CheckCircle2Icon,
  ChevronRightIcon,
  Loader2Icon,
  CopyIcon,
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
  TerminalHeader,
} from "@/components/ai-elements/terminal";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { ScrollArea } from "@/components/ui/scroll-area";
import { cn } from "@/lib/utils";
import type { ToolCallInfo } from "@/hooks/use-session-runtime";

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      className="shrink-0 p-0.5 rounded opacity-0 group-hover/in:opacity-100 transition-opacity text-muted-foreground hover:text-foreground"
      onClick={(e) => {
        e.stopPropagation();
        void navigator.clipboard.writeText(text).then(() => {
          setCopied(true);
          setTimeout(() => setCopied(false), 2000);
        });
      }}
      aria-label="Copy command"
    >
      {copied ? <CheckCircle2Icon className="size-3" /> : <CopyIcon className="size-3" />}
    </button>
  );
}

function StatusBullet({ status }: { status: "running" | "complete" | "error" }) {
  if (status === "running") {
    return <span className="inline-block size-2 rounded-full shrink-0 bg-primary animate-pulse" />;
  }
  return (
    <span className={cn(
      "inline-block size-2 rounded-full shrink-0",
      status === "error" ? "bg-red-500" : "bg-emerald-500",
    )} />
  );
}

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
  const [dialogOpen, setDialogOpen] = useState(false);
  const contentRef = useRef<HTMLDivElement>(null);
  const [overflows, setOverflows] = useState(false);

  useEffect(() => {
    if (contentRef.current) {
      setOverflows(contentRef.current.scrollHeight > COLLAPSED_HEIGHT);
    }
  }, [output]);

  const terminalContent = [
    result.command && `${result.command}`,
    output ? `\n${output}` : (!isRunning ? "\n(no output)" : ""),
  ].filter(Boolean).join("");

  return (
    <>
      <Terminal
        output={terminalContent}
        isStreaming={isRunning}
        className="group/term relative w-full text-xs"
      >
        <TerminalHeader className="py-1.5 px-2">
          <div className="flex items-center gap-1.5 min-w-0 text-sm font-mono">
            <StatusBullet status={isRunning ? "running" : exitOk ? "complete" : "error"} />
            <span className="font-semibold text-foreground shrink-0">Bash</span>
          </div>
        </TerminalHeader>
        {result.command && (
          <div className="group/in flex items-start gap-2 bg-background text-muted-foreground px-3 pt-2 font-mono text-xs leading-relaxed">
            <span className="shrink-0 select-none text-emerald-600">IN</span>
            <pre className="whitespace-pre-wrap wrap-break-word text-foreground/90 flex-1">{result.command}</pre>
            <CopyButton text={result.command} />
          </div>
        )}
        <div
          ref={contentRef}
          role="button"
          tabIndex={0}
          onClick={() => overflows && setDialogOpen(true)}
          onKeyDown={(e) => { if (e.key === "Enter" && overflows) setDialogOpen(true); }}
          className={cn(
            "overflow-hidden px-3 py-2 bg-background font-mono text-xs leading-relaxed max-h-16",
            overflows && "cursor-pointer",
          )}
        >
          {(output || !isRunning) && (
            <div className="flex gap-2 text-muted-foreground">
              <span className="shrink-0 select-none text-sky-600">OUT</span>
              <pre className="whitespace-pre-wrap wrap-break-word">
                {output || <span className="text-muted-foreground/60">(no output)</span>}
              </pre>
            </div>
          )}
          {isRunning && !output && (
            <span className="ml-0.5 inline-block h-3.5 w-1.5 animate-pulse bg-foreground" />
          )}
        </div>
        {overflows && (
          <Button
            variant="outline"
            size="xs"
            onClick={() => setDialogOpen(true)}
            className="absolute bottom-1.5 right-2 opacity-0 group-hover/term:opacity-100 transition-opacity backdrop-blur-sm"
          >
            Expand
          </Button>
        )}
      </Terminal>

      {/* Full output dialog */}
      <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
        <DialogContent className="sm:max-w-[50vw] w-full h-[70vh] flex flex-col p-0 gap-0 overflow-hidden">
          <DialogHeader className="px-4 py-3 border-b border-border shrink-0">
            <DialogTitle>&nbsp;</DialogTitle>
          </DialogHeader>
          <ScrollArea className="flex-1 min-h-0">
            <pre className="px-4 py-3 font-mono text-xs leading-relaxed whitespace-pre-wrap wrap-break-word">
              {output || "(no output)"}
            </pre>
          </ScrollArea>
        </DialogContent>
      </Dialog>
    </>
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
    <Queue>
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

export function ToolCallBlock({ tc, onFileSelect }: { tc: ToolCallInfo; onFileSelect?: (path: string) => void }) {
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
      return (
        <Terminal output="" isStreaming className="w-full text-xs">
          <TerminalHeader className="py-1.5 px-3">
            <div className="flex items-center gap-1.5 min-w-0 text-xs">
              <StatusBullet status="running" />
              <span className="font-semibold text-foreground shrink-0">Bash</span>
            </div>
          </TerminalHeader>
        </Terminal>
      );
    }
  }

  // Web extract / web search / web crawl — compact one-liner with URL.
  if (["web_extract", "web_search", "web_crawl"].includes(tc.toolName)) {
    let displayText = "";
    try {
      const args = JSON.parse(tc.args);
      if (tc.toolName === "web_extract") {
        const urls: string[] = args.urls ?? [];
        displayText = urls[0] ?? "";
      } else if (tc.toolName === "web_search") {
        displayText = args.query ?? "";
      } else if (tc.toolName === "web_crawl") {
        displayText = args.url ?? "";
      }
    } catch { /* ignore */ }

    const toolLabel = {
      web_extract: "Web Fetch",
      web_search: "Web Search",
      web_crawl: "Web Crawl",
    }[tc.toolName] ?? tc.toolName;

    return (
      <div className="flex items-center gap-1.5 px-2 py-1 text-sm font-mono">
        <StatusBullet status={tc.status} />
        <span className="font-semibold text-foreground">{toolLabel}</span>
        <span className="text-muted-foreground truncate">{displayText}</span>
      </div>
    );
  }

  // Read/write/patch/search/list file tools — compact one-liner.
  if (["read_file", "write_file", "patch", "search_files", "list_files"].includes(tc.toolName)) {
    let filePath = "";
    let detail = "";
    try {
      const args = JSON.parse(tc.args);
      filePath = args.path ?? args.file_path ?? "";
      if (tc.toolName === "read_file") {
        const parts: string[] = [];
        if (args.offset != null || args.limit != null) {
          const start = (args.offset ?? 0) + 1;
          const end = args.limit ? start + args.limit - 1 : undefined;
          parts.push(end ? `lines ${start}-${end}` : `from line ${start}`);
        }
        if (parts.length) detail = `(${parts.join(", ")})`;
      } else if (tc.toolName === "search_files") {
        detail = args.pattern ? `"${args.pattern}"` : "";
      } else if (tc.toolName === "list_files") {
        detail = args.pattern && args.pattern !== "*" ? `"${args.pattern}"` : "";
      }
    } catch { /* ignore */ }

    const toolLabel = {
      read_file: "Read",
      write_file: "Write",
      patch: "Patch",
      search_files: "Search",
      list_files: "List",
    }[tc.toolName] ?? tc.toolName;

    // The backend replaces the workspace path with __WORKSPACE__.
    // Also handle legacy events with absolute paths by stripping
    // everything up to a recognizable project directory.
    const displayPath = (() => {
      // New format: __WORKSPACE__/path
      if (filePath.startsWith("__WORKSPACE__")) {
        return filePath.replace(/^__WORKSPACE__\/?/, "");
      }
      // Legacy: absolute path — strip /home/user/... prefix.
      if (filePath.startsWith("/")) {
        const parts = filePath.split("/").filter(Boolean);
        // Skip system dirs, usernames, workspace UUIDs.
        const uuidRe = /^[0-9a-f]{8}-/;
        const skip = new Set(["home", "tmp", "work", "var", "opt", "data", "surogates", "workspaces"]);
        for (let i = 0; i < parts.length - 1; i++) {
          if (!skip.has(parts[i]) && !uuidRe.test(parts[i])) {
            return parts.slice(i).join("/");
          }
        }
        return parts.slice(-2).join("/");
      }
      return filePath;
    })();

    return (
      <div className="flex items-center gap-1.5 px-2 py-1 text-sm font-mono">
        <StatusBullet status={tc.status} />
        <span className="font-semibold text-foreground">{toolLabel}</span>
        {onFileSelect && filePath && ["read_file", "write_file", "patch"].includes(tc.toolName) ? (
          <button
            type="button"
            onClick={() => onFileSelect(filePath)}
            className="text-primary hover:underline truncate text-left cursor-pointer underline"
          >
            {displayPath}
          </button>
        ) : (
          <span className="text-muted-foreground truncate">{displayPath}</span>
        )}
        {detail && <span className="text-muted-foreground ml-1">{detail}</span>}
      </div>
    );
  }

  // Default — generic collapsible tool call.
  return (
    <div>
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
        <StatusBullet status={tc.status} />
        <span className="font-medium text-foreground/80">{tc.toolName}</span>
      </button>

      {expanded && (
        <div className="ml-6 mt-0.5 space-y-1 text-sm font-mono">
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
