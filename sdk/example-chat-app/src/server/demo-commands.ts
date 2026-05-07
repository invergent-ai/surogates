import type {
  ExampleArtifactPayload,
  ExampleSlashCommand,
} from "../shared/types";
import type {
  ExampleSessionRecord,
  ExampleSessionStore,
} from "./session-store";

export const HARNESS_BUILTIN_TOOL_NAMES = [
  "memory",
  "skills_list",
  "skill_view",
  "skill_manage",
  "session_search",
  "web_search",
  "web_extract",
  "web_crawl",
  "clarify",
  "delegate_task",
  "todo",
  "process",
  "consult_expert",
  "create_artifact",
  "spawn_worker",
  "send_worker_message",
  "stop_worker",
  "terminal",
  "read_file",
  "write_file",
  "patch",
  "search_files",
  "list_files",
] as const;

interface DemoToolCall {
  id: string;
  name: string;
  arguments: Record<string, unknown>;
  result?: unknown;
}

export const DEMO_SLASH_COMMANDS: ExampleSlashCommand[] = [
  {
    value: "/demo-tools",
    label: "/demo-tools",
    description: "Show the harness tool surface and renderer payloads.",
  },
  {
    value: "/demo-artifacts",
    label: "/demo-artifacts",
    description: "Create markdown, table, chart, HTML, and SVG artifacts.",
  },
  {
    value: "/demo-clarify",
    label: "/demo-clarify",
    description: "Show an interactive clarify tool call.",
  },
  {
    value: "/demo-expert",
    label: "/demo-expert",
    description: "Show consult_expert output and feedback controls.",
  },
  {
    value: "/demo-errors",
    label: "/demo-errors",
    description: "Show policy and provider error states.",
  },
  {
    value: "/demo-context",
    label: "/demo-context",
    description: "Clear the visible conversation through context.compact.",
  },
];

export function runDemoCommand(
  store: ExampleSessionStore,
  record: ExampleSessionRecord,
  command: string,
) {
  switch (command.trim().split(/\s+/)[0]) {
    case "/demo-tools":
      emitToolDemo(record);
      return true;
    case "/demo-artifacts":
      emitArtifactDemo(store, record);
      return true;
    case "/demo-clarify":
      emitClarifyDemo(record);
      return true;
    case "/demo-expert":
      emitExpertDemo(record);
      return true;
    case "/demo-errors":
      emitErrorDemo(record);
      return true;
    case "/demo-context":
      record.events.append("context.compact", { strategy: "clear" });
      record.events.append("session.done", {});
      return true;
    default:
      return false;
  }
}

export function submitClarifyDemo(
  record: ExampleSessionRecord,
  toolCallId: string,
  responses: unknown[],
) {
  record.events.append("clarify.response", {
    tool_call_id: toolCallId,
    responses,
  });
  record.events.append("llm.delta", {
    content: "Thanks, the clarify response was received by the example backend.",
  });
  record.events.append("llm.response", {
    message: { content: "Thanks, the clarify response was received by the example backend." },
    model: record.session.model,
  });
  record.events.append("session.done", {});
}

export function submitExpertFeedbackDemo(
  record: ExampleSessionRecord,
  input: { targetEventId: number; rating: "up" | "down"; reason?: string },
) {
  record.events.append(input.rating === "up" ? "expert.endorse" : "expert.override", {
    target_event_id: input.targetEventId,
    reason: input.reason,
  });
}

function emitToolDemo(record: ExampleSessionRecord) {
  record.events.append("llm.delta", { content: "Running the harness tool demo." });
  emitScriptedTools(record, [
    {
      id: "demo-terminal",
      name: "terminal",
      arguments: { command: "pnpm test" },
      result: {
        output: "5 test files passed in the example app.",
        exit_code: 0,
        error: null,
      },
    },
    {
      id: "demo-read-file",
      name: "read_file",
      arguments: { path: "README.md" },
      result: "# Example workspace\n\nFiles here are stored in memory.",
    },
    {
      id: "demo-write-file",
      name: "write_file",
      arguments: {
        path: "notes/summary.md",
        content: "Example write payload.",
      },
      result: { success: true, bytes_written: 22 },
    },
    {
      id: "demo-patch",
      name: "patch",
      arguments: {
        path: "src/example.ts",
        old_string: "const enabled = false;",
        new_string: "const enabled = true;",
      },
      result: { success: true, replacements: 1 },
    },
    {
      id: "demo-search-files",
      name: "search_files",
      arguments: {
        path: ".",
        pattern: "AgentChat",
      },
      result: {
        matches: [
          { path: "README.md", line: 3, text: "AgentChat example" },
          { path: "src/client/App.tsx", line: 28, text: "AgentChat" },
        ],
      },
    },
    {
      id: "demo-list-files",
      name: "list_files",
      arguments: {
        path: "src",
        pattern: "*.ts",
      },
      result: {
        entries: [
          { name: "adapter.ts", path: "src/client/adapter.ts", kind: "file" },
          { name: "server", path: "src/server", kind: "dir" },
        ],
      },
    },
    {
      id: "demo-todo",
      name: "todo",
      arguments: { action: "list" },
      result: {
        todos: [
          {
            id: "inspect",
            content: "Inspect renderer contracts",
            status: "completed",
          },
          {
            id: "verify",
            content: "Verify demo coverage",
            status: "in_progress",
          },
        ],
      },
    },
    {
      id: "demo-execute-code",
      name: "execute_code",
      arguments: {
        code: "print('hello from execute_code')",
      },
      result: {
        stdout: "hello from execute_code\n",
        stderr: "",
        exit_code: 0,
      },
    },
    {
      id: "demo-session-search",
      name: "session_search",
      arguments: { query: "example app architecture" },
      result: {
        results: [
          { session_id: record.session.id, score: 0.92, snippet: "Example app architecture notes." },
        ],
      },
    },
    {
      id: "demo-web-search",
      name: "web_search",
      arguments: { query: "OpenAI compatible chat completions streaming" },
      result: {
        results: [
          { title: "Streaming chat completions", url: "https://example.com/streaming" },
        ],
      },
    },
    {
      id: "demo-web-extract",
      name: "web_extract",
      arguments: { urls: ["https://example.com/docs"] },
      result: { pages: [{ url: "https://example.com/docs", title: "Docs" }] },
    },
    {
      id: "demo-web-crawl",
      name: "web_crawl",
      arguments: { url: "https://example.com" },
      result: { crawled: 3 },
    },
    {
      id: "demo-skills-list",
      name: "skills_list",
      arguments: { category: "examples" },
      result: {
        success: true,
        count: 2,
        categories: ["examples"],
      },
    },
    {
      id: "demo-skill-view",
      name: "skill_view",
      arguments: { name: "example-chat" },
      result: {
        success: true,
        name: "example-chat",
        token_estimate: 420,
      },
    },
    {
      id: "demo-skill-manage",
      name: "skill_manage",
      arguments: {
        action: "patch",
        name: "example-chat",
        old_string: "old instruction",
        new_string: "updated instruction",
      },
      result: {
        success: true,
        message: "Skill patched for the demo.",
      },
    },
    {
      id: "demo-memory",
      name: "memory",
      arguments: {
        action: "add",
        target: "memory",
        content: "The user prefers concise examples.",
      },
      result: {
        success: true,
        message: "Memory stored for this demo session.",
        usage: "1 entry",
      },
    },
    {
      id: "demo-process",
      name: "process",
      arguments: { action: "list" },
      result: {
        processes: [
          {
            session_id: "proc-demo",
            command: "pnpm -C sdk/example-chat-app dev",
            pid: 12345,
            uptime_seconds: 42,
            status: "running",
          },
        ],
      },
    },
    {
      id: "demo-delegate-task",
      name: "delegate_task",
      arguments: {
        agent_type: "reviewer",
        goal: "Review the example app architecture.",
        context: "This is a scripted demo payload.",
      },
      result: "The delegated reviewer found no structural issues.",
    },
    {
      id: "demo-spawn-worker",
      name: "spawn_worker",
      arguments: {
        agent_type: "reviewer",
        goal: "Review the example app architecture asynchronously.",
      },
      result: {
        worker_id: "worker-demo",
        status: "queued",
      },
    },
    {
      id: "demo-send-worker-message",
      name: "send_worker_message",
      arguments: {
        worker_id: "worker-demo",
        message: "Please also check the streaming adapter.",
      },
      result: {
        worker_id: "worker-demo",
        status: "queued",
      },
    },
    {
      id: "demo-stop-worker",
      name: "stop_worker",
      arguments: {
        worker_id: "worker-demo",
        reason: "Scripted demo stop.",
      },
      result: {
        worker_id: "worker-demo",
        status: "stopped",
      },
    },
  ]);
  record.events.append("llm.response", {
    message: { content: "The scripted tool demo completed." },
    model: record.session.model,
  });
  record.events.append("session.done", {});
}

function emitArtifactDemo(store: ExampleSessionStore, record: ExampleSessionRecord) {
  const artifacts: ExampleArtifactPayload[] = [
    {
      kind: "markdown",
      meta: meta(record.session.id, "demo-markdown", "Markdown brief", "markdown", 72),
      spec: { content: "## Demo artifact\n\nThis markdown artifact came from a scripted event." },
    },
    {
      kind: "table",
      meta: meta(record.session.id, "demo-table", "Status table", "table", 120),
      spec: {
        columns: ["Feature", "Status"],
        rows: [
          { Feature: "Streaming chat", Status: "Real LLM" },
          { Feature: "Artifacts", Status: "Scripted demo" },
        ],
        caption: "Example feature coverage",
      },
    },
    {
      kind: "chart",
      meta: meta(record.session.id, "demo-chart", "Token chart", "chart", 300),
      spec: {
        vega_lite: {
          mark: "bar",
          data: { values: [{ type: "input", tokens: 12 }, { type: "output", tokens: 28 }] },
          encoding: {
            x: { field: "type", type: "nominal" },
            y: { field: "tokens", type: "quantitative" },
          },
        },
        caption: "Synthetic token usage",
      },
    },
    {
      kind: "html",
      meta: meta(record.session.id, "demo-html", "HTML card", "html", 96),
      spec: { html: "<strong>HTML artifact</strong><p>Rendered in a sandboxed frame.</p>" },
    },
    {
      kind: "svg",
      meta: meta(record.session.id, "demo-svg", "SVG badge", "svg", 140),
      spec: {
        svg: '<svg viewBox="0 0 220 80" xmlns="http://www.w3.org/2000/svg"><rect width="220" height="80" rx="8" fill="#0f766e"/><text x="110" y="48" text-anchor="middle" font-size="22" fill="white">AgentChat</text></svg>',
      },
    },
  ];
  for (const artifact of artifacts) {
    record.events.append("tool.call", {
      tool_call_id: `demo-create-${artifact.meta.artifact_id}`,
      name: "create_artifact",
      arguments: {
        name: artifact.meta.name,
        kind: artifact.kind,
      },
    });
    record.events.append("tool.result", {
      tool_call_id: `demo-create-${artifact.meta.artifact_id}`,
      content: JSON.stringify({
        artifact_id: artifact.meta.artifact_id,
        success: true,
      }),
    });
    store.addArtifact(record, artifact);
    record.events.append("artifact.created", {
      artifact_id: artifact.meta.artifact_id,
      name: artifact.meta.name,
      kind: artifact.kind,
      version: artifact.meta.version,
      size: artifact.meta.size,
    });
  }
  record.events.append("session.done", {});
}

function emitScriptedTools(record: ExampleSessionRecord, tools: DemoToolCall[]) {
  for (const tool of tools) {
    record.events.append("tool.call", {
      tool_call_id: tool.id,
      name: tool.name,
      arguments: tool.arguments,
    });
    if (tool.result !== undefined) {
      record.events.append("tool.result", {
        tool_call_id: tool.id,
        content: typeof tool.result === "string"
          ? tool.result
          : JSON.stringify(tool.result),
      });
    }
  }
}

function emitClarifyDemo(record: ExampleSessionRecord) {
  record.events.append("tool.call", {
    tool_call_id: "demo-clarify",
    name: "clarify",
    arguments: {
      questions: [
        {
          prompt: "Which output format should the example continue with?",
          choices: [
            { label: "Short", description: "A concise answer." },
            { label: "Detailed", description: "A fuller answer with examples." },
          ],
          allow_other: true,
        },
      ],
    },
  });
}

function emitExpertDemo(record: ExampleSessionRecord) {
  record.events.append("tool.call", {
    tool_call_id: "demo-expert",
    name: "consult_expert",
    arguments: {
      expert: "Architecture reviewer",
      question: "Review the example app architecture.",
    },
  });
  record.events.append("tool.result", {
    tool_call_id: "demo-expert",
    content: "The architecture is appropriate for an SDK example.",
  });
  record.events.append("expert.result", {
    summary: "The architecture is appropriate for an SDK example.",
  });
  record.events.append("session.done", {});
}

function emitErrorDemo(record: ExampleSessionRecord) {
  record.events.append("policy.denied", {
    reason: "Demo policy denied this synthetic destructive action.",
  });
  record.events.append("session.fail", {
    error_category: "provider_error",
    error_title: "Demo provider error",
    error_detail: "This scripted error shows how agent-chat-react renders failures.",
    retryable: true,
  });
}

function meta(
  sessionId: string,
  id: string,
  name: string,
  kind: ExampleArtifactPayload["kind"],
  size: number,
) {
  return {
    artifact_id: id,
    session_id: sessionId,
    name,
    kind,
    version: 1,
    size,
    created_at: new Date().toISOString(),
  };
}
