import { nanoid } from "nanoid";
import type {
  ExampleArtifactPayload,
  ExampleSession,
  ExampleSessionList,
  ExampleWorkspaceFile,
  ExampleWorkspaceTree,
  ExampleWorkspaceUpload,
} from "../shared/types";
import { SessionEventLog } from "./events";

export interface ChatTurn {
  role: "user" | "assistant";
  content: string;
}

export interface ExampleSessionRecord {
  session: ExampleSession;
  events: SessionEventLog;
  history: ChatTurn[];
  artifacts: Map<string, ExampleArtifactPayload>;
  workspace: Map<string, ExampleWorkspaceFile>;
  lastUserMessage: string | null;
}

export class ExampleSessionStore {
  private readonly sessions = new Map<string, ExampleSessionRecord>();

  list(limit = 50, offset = 0): ExampleSessionList {
    const sessions = [...this.sessions.values()]
      .map((record) => record.session)
      .sort((a, b) => stringValue(b.updatedAt).localeCompare(stringValue(a.updatedAt)));
    return {
      sessions: sessions.slice(offset, offset + limit),
      total: sessions.length,
    };
  }

  create(input: { agentId?: string; system?: string } = {}) {
    const now = new Date().toISOString();
    const id = `sess_${nanoid(10)}`;
    const session: ExampleSession = {
      id,
      agentId: input.agentId ?? null,
      channel: "example-chat-app",
      status: "active",
      title: "New chat",
      model: process.env.OPENAI_MODEL ?? "gpt-4o-mini",
      messageCount: 0,
      toolCallCount: 0,
      inputTokens: 0,
      outputTokens: 0,
      createdAt: now,
      updatedAt: now,
    };
    const record: ExampleSessionRecord = {
      session,
      events: new SessionEventLog(),
      history: input.system ? [{ role: "assistant", content: input.system }] : [],
      artifacts: new Map(),
      workspace: seedWorkspace(),
      lastUserMessage: null,
    };
    record.events.append("session.start", { session_id: id });
    this.sessions.set(id, record);
    return record;
  }

  require(sessionId: string) {
    const record = this.sessions.get(sessionId);
    if (!record) throw Object.assign(new Error("Session not found"), { status: 404 });
    return record;
  }

  delete(sessionId: string) {
    this.sessions.delete(sessionId);
  }

  touch(record: ExampleSessionRecord) {
    record.session.updatedAt = new Date().toISOString();
  }

  setStatus(record: ExampleSessionRecord, status: string) {
    record.session.status = status;
    this.touch(record);
  }

  addArtifact(record: ExampleSessionRecord, payload: ExampleArtifactPayload) {
    record.artifacts.set(payload.meta.artifact_id, payload);
  }

  getArtifact(record: ExampleSessionRecord, artifactId: string) {
    const artifact = record.artifacts.get(artifactId);
    if (!artifact) throw Object.assign(new Error("Artifact not found"), { status: 404 });
    return artifact;
  }

  getWorkspaceTree(record: ExampleSessionRecord): ExampleWorkspaceTree {
    const root: ExampleWorkspaceTree = {
      root: "workspace",
      entries: [],
      truncated: false,
    };
    const dirs = new Map<string, ExampleWorkspaceTree["entries"][number]>();
    for (const file of [...record.workspace.values()].sort((a, b) => a.path.localeCompare(b.path))) {
      const parts = file.path.split("/");
      let entries = root.entries;
      let currentPath = "";
      for (let index = 0; index < parts.length; index++) {
        const part = parts[index]!;
        currentPath = currentPath ? `${currentPath}/${part}` : part;
        const isFile = index === parts.length - 1;
        if (isFile) {
          entries.push({ name: part, path: currentPath, kind: "file", size: file.size });
        } else {
          let dir = dirs.get(currentPath);
          if (!dir) {
            dir = { name: part, path: currentPath, kind: "dir", children: [] };
            dirs.set(currentPath, dir);
            entries.push(dir);
          }
          entries = dir.children ?? [];
        }
      }
    }
    return root;
  }

  getWorkspaceFile(record: ExampleSessionRecord, path: string) {
    const file = record.workspace.get(normalizePath(path));
    if (!file) throw Object.assign(new Error("Workspace file not found"), { status: 404 });
    return file;
  }

  uploadWorkspaceFile(record: ExampleSessionRecord, input: {
    path: string;
    content: string;
    mimeType?: string | null;
  }): ExampleWorkspaceUpload {
    const path = normalizePath(input.path);
    const file: ExampleWorkspaceFile = {
      path,
      content: input.content,
      size: new TextEncoder().encode(input.content).byteLength,
      mime_type: input.mimeType ?? "text/plain",
      encoding: "utf-8",
      truncated: false,
    };
    record.workspace.set(path, file);
    this.touch(record);
    return { path, size: file.size };
  }

  deleteWorkspaceFile(record: ExampleSessionRecord, path: string) {
    record.workspace.delete(normalizePath(path));
    this.touch(record);
  }
}

function seedWorkspace() {
  const workspace = new Map<string, ExampleWorkspaceFile>();
  for (const file of [
    {
      path: "README.md",
      content: "# Example workspace\n\nFiles here are stored in memory by the demo backend.\n",
      mime_type: "text/markdown",
    },
    {
      path: "src/hello.ts",
      content: "export function hello(name: string) {\n  return `Hello, ${name}`;\n}\n",
      mime_type: "text/typescript",
    },
  ]) {
    workspace.set(file.path, {
      ...file,
      size: new TextEncoder().encode(file.content).byteLength,
      encoding: "utf-8",
      truncated: false,
    });
  }
  return workspace;
}

export function normalizePath(path: string) {
  return path.replace(/^\/+/, "").replace(/\/+/g, "/") || "uploaded.txt";
}

function stringValue(value: unknown) {
  return typeof value === "string" ? value : "";
}
