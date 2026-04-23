// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import { useState } from "react";
import {
  CheckCircle2Icon,
  ChevronDownIcon,
  CopyIcon,
} from "lucide-react";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { Shimmer } from "@/components/ai-elements/shimmer";
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

interface TerminalBodyProps {
  command: string;
  output: string;
  isRunning: boolean;
}

function TerminalCollapsible({ command, output, isRunning }: TerminalBodyProps) {
  // Terminal stays collapsed until the user clicks the trigger -- unlike
  // Reasoning, the terminal's output is rarely scannable at a glance and
  // auto-expanding every call floods the chat with noise.  The trigger
  // still shows a shimmer while running so progress is visible.
  const [isOpen, setIsOpen] = useState(false);

  return (
    <Collapsible
      open={isOpen}
      onOpenChange={setIsOpen}
      className="not-prose w-full"
    >
      <CollapsibleTrigger className="group/trigger flex w-fit items-center gap-2 text-sm transition-colors">
        <span className="text-left">
          {isRunning ? (
            <Shimmer as="span" duration={1}>Running command...</Shimmer>
          ) : (
            <span className="font-semibold text-foreground">Command result</span>
          )}
        </span>
        <ChevronDownIcon
          className={cn(
            "size-4 shrink-0 transition-transform",
            isOpen ? "rotate-180" : "rotate-0",
          )}
        />
      </CollapsibleTrigger>
      <CollapsibleContent
        className={cn(
          "mt-2 overflow-hidden",
          "data-[state=closed]:fade-out-0 data-[state=closed]:slide-out-to-top-2 data-[state=open]:slide-in-from-top-2 outline-none data-[state=closed]:animate-out data-[state=open]:animate-in",
        )}
      >
        <div className="rounded-lg border border-border overflow-hidden">
          {command && (
            <div className="group/in flex items-start gap-2 bg-background text-muted-foreground px-3 py-2 font-mono text-sm leading-relaxed">
              <span className="shrink-0 select-none text-emerald-600">IN</span>
              <pre className="whitespace-pre-wrap wrap-break-word text-foreground/90 flex-1">{command}</pre>
              <CopyButton text={command} />
            </div>
          )}
          <div
            className={cn(
              "px-3 py-2 bg-background font-mono text-sm leading-relaxed max-h-96 overflow-auto",
              command && "border-t border-border",
            )}
          >
            {(output || !isRunning) && (
              <div className="flex gap-2 text-foreground/90">
                <span className="shrink-0 select-none text-sky-600">OUT</span>
                <pre className="whitespace-pre-wrap wrap-break-word flex-1">
                  {output || <span className="text-muted-foreground/60">(no output)</span>}
                </pre>
              </div>
            )}
            {isRunning && !output && (
              <span className="ml-0.5 inline-block h-3.5 w-1.5 animate-pulse bg-foreground" />
            )}
          </div>
        </div>
      </CollapsibleContent>
    </Collapsible>
  );
}

export function TerminalToolBlock({ tc }: { tc: ToolCallInfo }) {
  const isRunning = tc.status === "running";
  const result = parseTerminalResult(tc.result, tc.args);

  if (result) {
    const output = result.output || result.error || "";
    return (
      <TerminalCollapsible
        command={result.command}
        output={output}
        isRunning={isRunning}
      />
    );
  }

  if (isRunning) {
    let command = "";
    try {
      const parsedArgs = JSON.parse(tc.args);
      command = parsedArgs?.command ?? "";
    } catch { /* ignore */ }

    return <TerminalCollapsible command={command} output="" isRunning />;
  }

  return null;
}
