// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Renders a `/code` coding-agent run: streamed (ansi-aware) progress, the
// final message, token usage, and an error state. Mirrors the terminal
// tool's collapsible shell.
import { useState } from "react";
import AnsiImport from "ansi-to-react";
import { ChevronDownIcon } from "lucide-react";

// ansi-to-react is a CJS module ({ default: Component, __esModule: true }).
// Under the SDK's tsup -> consumer-bundler interop the default import can
// arrive wrapped as { default: Component } rather than the component itself,
// which makes React throw "Element type is invalid ... got: object". Unwrap
// defensively so it works regardless of how the consumer bundles us.
const Ansi = ((AnsiImport as unknown as { default?: unknown }).default ??
  AnsiImport) as typeof AnsiImport;
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "../../ui/collapsible";
import { Shimmer } from "../../ai-elements/shimmer";
import { cn } from "../../../lib/utils";
import type { ToolCallInfo } from "../../../types";

interface CodeRunState {
  agent: string;
  provider: string;
  prompt: string;
  output: string;
  finalMessage: string;
  error: string | null;
  inputTokens: number;
  outputTokens: number;
}

function parseCodeRun(result: string | undefined, args: string): CodeRunState {
  const fallback: CodeRunState = {
    agent: "",
    provider: "",
    prompt: "",
    output: "",
    finalMessage: "",
    error: null,
    inputTokens: 0,
    outputTokens: 0,
  };
  if (result) {
    try {
      const parsed = JSON.parse(result) as Partial<CodeRunState>;
      return { ...fallback, ...parsed, error: parsed.error ?? null };
    } catch {
      /* fall through to args */
    }
  }
  try {
    const parsedArgs = JSON.parse(args) as Partial<CodeRunState>;
    return { ...fallback, ...parsedArgs, error: null };
  } catch {
    return fallback;
  }
}

function agentLabel(state: CodeRunState): string {
  if (state.agent) return state.agent;
  if (state.provider === "anthropic") return "claude";
  if (state.provider === "openai") return "codex";
  return "coding agent";
}

export function CodeRunToolBlock({
  tc,
  defaultOpen = false,
}: {
  tc: ToolCallInfo;
  defaultOpen?: boolean;
}) {
  const [isOpen, setIsOpen] = useState(defaultOpen);
  const isRunning = tc.status === "running";
  const state = parseCodeRun(tc.result, tc.args);
  const label = agentLabel(state);
  const hasError = tc.status === "error" || Boolean(state.error);

  return (
    <Collapsible
      open={isOpen}
      onOpenChange={setIsOpen}
      className="not-prose w-full"
    >
      <CollapsibleTrigger className="group/trigger flex w-fit items-center gap-2 text-sm transition-colors">
        <span className="text-left">
          {isRunning ? (
            <Shimmer as="span" duration={1}>{`Running ${label}...`}</Shimmer>
          ) : hasError ? (
            <span className="font-semibold text-destructive">{`${label} failed`}</span>
          ) : (
            <span className="font-semibold text-foreground">{`Ran ${label}`}</span>
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
          {state.prompt && (
            <div className="flex items-start gap-2 bg-background text-muted-foreground px-3 py-2 font-mono text-sm leading-relaxed">
              <span className="shrink-0 select-none text-emerald-600">IN</span>
              <pre className="whitespace-pre-wrap wrap-break-word text-foreground/90 flex-1">
                {state.prompt}
              </pre>
            </div>
          )}
          <div
            className={cn(
              "px-3 py-2 bg-background font-mono text-sm leading-relaxed max-h-96 overflow-auto",
              state.prompt && "border-t border-border",
            )}
          >
            {(state.output || !isRunning) && (
              <div className="flex gap-2 text-foreground/90">
                <span className="shrink-0 select-none text-sky-600">OUT</span>
                <pre className="whitespace-pre-wrap wrap-break-word flex-1">
                  {state.output ? (
                    <Ansi>{state.output}</Ansi>
                  ) : (
                    <span className="text-muted-foreground/60">(no output)</span>
                  )}
                  {isRunning && (
                    <span className="ml-0.5 inline-block h-3.5 w-1.5 animate-pulse bg-foreground" />
                  )}
                </pre>
              </div>
            )}
            {isRunning && !state.output && (
              <span className="ml-0.5 inline-block h-3.5 w-1.5 animate-pulse bg-foreground" />
            )}
          </div>

          {!isRunning && hasError && state.error && (
            <div className="border-t border-border bg-destructive/5 px-3 py-2 text-sm text-destructive">
              <pre className="whitespace-pre-wrap wrap-break-word">{state.error}</pre>
            </div>
          )}

          {!isRunning && !hasError && state.finalMessage && (
            <div className="border-t border-border bg-background px-3 py-2 text-sm text-foreground/90">
              <pre className="whitespace-pre-wrap wrap-break-word">{state.finalMessage}</pre>
            </div>
          )}

          {!isRunning && (state.inputTokens > 0 || state.outputTokens > 0) && (
            <div className="border-t border-border bg-background px-3 py-1.5 text-xs text-muted-foreground">
              {`${state.inputTokens.toLocaleString()} in · ${state.outputTokens.toLocaleString()} out tokens`}
            </div>
          )}
        </div>
      </CollapsibleContent>
    </Collapsible>
  );
}
