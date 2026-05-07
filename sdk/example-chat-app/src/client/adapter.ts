import type {
  AgentChatAdapter,
  AgentChatEventStream,
  AgentChatEventType,
  AgentChatSseMessageEvent,
} from "@invergent/agent-chat-react";

export function createExampleChatAdapter(baseUrl = "/api"): AgentChatAdapter {
  return {
    async listSessions(input) {
      const params = new URLSearchParams();
      if (input.limit != null) params.set("limit", String(input.limit));
      if (input.offset != null) params.set("offset", String(input.offset));
      return getJson(`${baseUrl}/sessions${query(params)}`);
    },
    async createSession(input) {
      return postJson(`${baseUrl}/sessions`, input);
    },
    async getSession(input) {
      return getJson(`${baseUrl}/sessions/${encodeURIComponent(input.sessionId)}`);
    },
    async sendMessage(input) {
      return postJson(`${baseUrl}/sessions/${encodeURIComponent(input.sessionId)}/messages`, {
        content: input.content,
      });
    },
    async pauseSession(input) {
      await postJson(`${baseUrl}/sessions/${encodeURIComponent(input.sessionId)}/pause`, {});
    },
    async retrySession(input) {
      return postJson(`${baseUrl}/sessions/${encodeURIComponent(input.sessionId)}/retry`, {});
    },
    async deleteSession(input) {
      await request(`${baseUrl}/sessions/${encodeURIComponent(input.sessionId)}`, {
        method: "DELETE",
      });
    },
    async getArtifact(input) {
      return getJson(
        `${baseUrl}/sessions/${encodeURIComponent(input.sessionId)}/artifacts/${encodeURIComponent(input.artifactId)}`,
      );
    },
    async submitClarifyResponse(input) {
      return postJson(
        `${baseUrl}/sessions/${encodeURIComponent(input.sessionId)}/clarify/${encodeURIComponent(input.toolCallId)}`,
        { responses: input.responses },
      );
    },
    async submitExpertFeedback(input) {
      return postJson(`${baseUrl}/sessions/${encodeURIComponent(input.sessionId)}/expert-feedback`, {
        expertResultEventId: input.expertResultEventId,
        rating: input.rating,
        reason: input.reason,
      });
    },
    async listSlashCommands() {
      return getJson(`${baseUrl}/slash-commands`);
    },
    async getWorkspaceTree(input) {
      return getJson(`${baseUrl}/sessions/${encodeURIComponent(input.sessionId)}/workspace/tree`);
    },
    async getWorkspaceFile(input) {
      const params = new URLSearchParams({ path: input.path });
      return getJson(
        `${baseUrl}/sessions/${encodeURIComponent(input.sessionId)}/workspace/file?${params}`,
      );
    },
    async uploadWorkspaceFile(input) {
      const body = new FormData();
      body.set("file", input.file);
      if (input.directory) body.set("directory", input.directory);
      return postForm(
        `${baseUrl}/sessions/${encodeURIComponent(input.sessionId)}/workspace/upload`,
        body,
      );
    },
    async deleteWorkspaceFile(input) {
      const params = new URLSearchParams({ path: input.path });
      await request(
        `${baseUrl}/sessions/${encodeURIComponent(input.sessionId)}/workspace/file?${params}`,
        { method: "DELETE" },
      );
    },
    openEventStream(input) {
      const params = new URLSearchParams({ after: String(input.after) });
      return wrapEventSource(
        new EventSource(
          `${baseUrl}/sessions/${encodeURIComponent(input.sessionId)}/events?${params}`,
        ),
      );
    },
  };
}

function wrapEventSource(source: EventSource): AgentChatEventStream {
  let errorHandler: (() => void) | null = null;
  return {
    addEventListener(
      type: AgentChatEventType,
      listener: (event: AgentChatSseMessageEvent) => void,
    ) {
      source.addEventListener(type, (event) => {
        const message = event as MessageEvent<string>;
        listener({ data: message.data, lastEventId: message.lastEventId });
      });
    },
    close() {
      source.close();
    },
    get onerror() {
      return errorHandler;
    },
    set onerror(handler: (() => void) | null) {
      errorHandler = handler;
      source.onerror = handler ? () => handler() : null;
    },
  };
}

async function getJson<T>(url: string): Promise<T> {
  const response = await request(url);
  return response.json() as Promise<T>;
}

async function postJson<T>(url: string, body: unknown): Promise<T> {
  const response = await request(url, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

async function postForm<T>(url: string, body: FormData): Promise<T> {
  const response = await request(url, { method: "POST", body });
  return response.json() as Promise<T>;
}

async function request(url: string, init?: RequestInit) {
  const response = await fetch(url, init);
  if (!response.ok) {
    const message = await readError(response);
    throw new Error(message);
  }
  return response;
}

async function readError(response: Response) {
  try {
    const body = (await response.json()) as { error?: string };
    return body.error ?? `${response.status} ${response.statusText}`;
  } catch {
    return `${response.status} ${response.statusText}`;
  }
}

function query(params: URLSearchParams) {
  const value = params.toString();
  return value ? `?${value}` : "";
}
