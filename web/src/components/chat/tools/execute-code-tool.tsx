// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import { CodeBlock, CodeBlockCopyButton } from "@/components/ai-elements/code-block";
import {
  Sandbox,
  SandboxContent,
  SandboxHeader,
  SandboxTabContent,
  SandboxTabs,
  SandboxTabsBar,
  SandboxTabsList,
  SandboxTabsTrigger,
} from "@/components/ai-elements/sandbox";
import type { ToolCallInfo } from "@/hooks/use-session-runtime";

interface ExecuteCodeResult {
  code: string;
  output: string;
  hasError: boolean;
}

function parseExecuteCodeResult(
  result: string | undefined,
  args: string,
): ExecuteCodeResult | null {
  let code = "";
  try {
    const parsedArgs = JSON.parse(args);
    code = parsedArgs?.code ?? "";
  } catch { /* ignore */ }

  if (!code) return null;

  let output = "";
  let hasError = false;
  if (result) {
    try {
      const parsed = JSON.parse(result);
      const stdout = parsed?.stdout ?? parsed?.output ?? "";
      const stderr = parsed?.stderr ?? parsed?.error ?? "";
      const exitCode = parsed?.exit_code ?? 0;
      hasError = exitCode !== 0 || !!stderr;
      output = stderr ? `${stdout}\n${stderr}`.trim() : stdout;
    } catch {
      output = result;
    }
  }

  return { code, output, hasError };
}

export function ExecuteCodeToolBlock({ tc }: { tc: ToolCallInfo }) {
  const isRunning = tc.status === "running";
  const result = parseExecuteCodeResult(tc.result, tc.args);
  if (!result) return null;

  const state = isRunning
    ? "input-available"
    : result.hasError
      ? "output-error"
      : "output-available";

  return (
    <Sandbox>
      <SandboxHeader state={state} title="Run code" />
      <SandboxContent>
        <SandboxTabs defaultValue="code">
          <SandboxTabsBar>
            <SandboxTabsList>
              <SandboxTabsTrigger value="code">Code</SandboxTabsTrigger>
              <SandboxTabsTrigger value="output">Output</SandboxTabsTrigger>
            </SandboxTabsList>
          </SandboxTabsBar>
          <SandboxTabContent value="code">
            <div className="max-h-60 overflow-auto">
              <CodeBlock
                className="border-0"
                code={result.code}
                language="python"
              >
                <CodeBlockCopyButton
                  className="absolute top-2 right-2 opacity-0 transition-opacity duration-200 group-hover:opacity-100"
                  size="sm"
                />
              </CodeBlock>
            </div>
          </SandboxTabContent>
          <SandboxTabContent value="output">
            <div className="max-h-60 overflow-auto">
              <CodeBlock
                className="border-0"
                code={result.output || (isRunning ? "Running..." : "(no output)")}
                language="log"
              >
                <CodeBlockCopyButton
                  className="absolute top-2 right-2 opacity-0 transition-opacity duration-200 group-hover:opacity-100"
                  size="sm"
                />
              </CodeBlock>
            </div>
          </SandboxTabContent>
        </SandboxTabs>
      </SandboxContent>
    </Sandbox>
  );
}
