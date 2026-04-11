// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import { useEffect, useRef, useState } from "react";
import { CheckCircle2Icon, CopyIcon } from "lucide-react";
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

interface TerminalResult {
  output: string;
  exit_code: number;
  error: string | null;
  command: string;
}

export function parseTerminalResult(
  result: string | undefined,
  args: string,
): TerminalResult | null {
  if (!result) return null;
  try {
    let parsed = JSON.parse(result);

    // Unwrap double-wrapped results: the sandbox may return
    // {"stdout": "{\"output\": \"...\", \"exit_code\": 0}"} where
    // stdout contains the actual tool handler result as a JSON string.
    if (typeof parsed?.stdout === "string" && parsed.stdout.startsWith("{")) {
      try {
        const inner = JSON.parse(parsed.stdout);
        if (inner?.output !== undefined || inner?.exit_code !== undefined) {
          parsed = inner;
        }
      } catch { /* not nested JSON, use as-is */ }
    }

    const hasOutput = typeof parsed?.output === "string";
    const hasStdout = typeof parsed?.stdout === "string";
    const hasExitCode = typeof parsed?.exit_code === "number";
    if (!hasOutput && !hasStdout && !hasExitCode) {
      return null;
    }
    let command = "";
    try {
      const parsedArgs = JSON.parse(args);
      command = parsedArgs?.command ?? "";
    } catch { /* ignore */ }
    const output = parsed.output ?? parsed.stdout ?? "";
    const stderr = parsed.stderr ?? parsed.error ?? "";
    const combined = stderr ? `${output}\n${stderr}`.trim() : output;
    return {
      output: combined,
      exit_code: parsed.exit_code ?? 0,
      error: parsed.error ?? null,
      command,
    };
  } catch {
    return null;
  }
}

const COLLAPSED_HEIGHT = 96;

function TerminalToolResult({ result, isRunning }: { result: TerminalResult; isRunning: boolean }) {
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
        className="group/term relative w-full text-sm"
      >
        <TerminalHeader>
          <div className="flex items-center gap-1.5 min-w-0 text-sm font-mono">
            <span className="font-semibold text-foreground shrink-0">Bash</span>
          </div>
        </TerminalHeader>
        {result.command && (
          <div className="group/in flex items-start gap-2 bg-background text-muted-foreground px-3 pt-2 font-mono text-sm leading-relaxed">
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
            "overflow-hidden px-3 py-2 bg-background font-mono text-sm leading-relaxed max-h-20",
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

      <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
        <DialogContent className="sm:max-w-[50vw] w-full h-[70vh] flex flex-col p-0 gap-0 overflow-hidden">
          <DialogHeader className="px-4 py-3 border-b border-border shrink-0">
            <DialogTitle>&nbsp;</DialogTitle>
          </DialogHeader>
          <ScrollArea className="flex-1 min-h-0">
            <pre className="px-4 py-3 font-mono text-sm leading-relaxed whitespace-pre-wrap wrap-break-word">
              {output || "(no output)"}
            </pre>
          </ScrollArea>
        </DialogContent>
      </Dialog>
    </>
  );
}

export function TerminalToolBlock({ tc }: { tc: ToolCallInfo }) {
  const isRunning = tc.status === "running";
  const result = parseTerminalResult(tc.result, tc.args);

  if (result) {
    return <TerminalToolResult result={result} isRunning={isRunning} />;
  }

  if (isRunning) {
    return (
      <Terminal output="" isStreaming className="w-full text-sm">
        <TerminalHeader>
          <div className="flex items-center gap-1.5 min-w-0 text-sm">
            <span className="font-semibold text-foreground shrink-0">Bash</span>
          </div>
        </TerminalHeader>
      </Terminal>
    );
  }

  return null;
}
