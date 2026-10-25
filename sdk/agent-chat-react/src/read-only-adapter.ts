import type { AgentChatAdapter } from "./types";

/** What a read-only adapter refuses with: every write, and every read its source does not have. */
export class ReadOnlyChatError extends Error {
  constructor() {
    super("This chat is read-only.");
    this.name = "ReadOnlyChatError";
  }
}

/** The reads a read-only chat is built from: a session and its events, and optionally what else it shows. */
export type AgentChatReadSource = Pick<AgentChatAdapter, "getSession" | "openEventStream">
  & Partial<Pick<
    AgentChatAdapter,
    | "pollEvents"
    | "getArtifact"
    | "getWorkspaceTree"
    | "getWorkspaceFile"
    | "getBrowserState"
    | "getChatViewMode"
  >>;

async function refuse(): Promise<never> {
  throw new ReadOnlyChatError();
}

/**
 * An adapter that only reads, from *source*. It takes from *source* only the reads
 * `AgentChatReadSource` names, so a write on a wider object never passes through. Every other
 * method the adapter must have refuses with `ReadOnlyChatError`; the optional ones it leaves out,
 * so the chat leaves their features off.
 */
export function readOnlyAdapter(source: AgentChatReadSource): AgentChatAdapter {
  return {
    getSession: source.getSession,
    openEventStream: source.openEventStream,
    ...(source.pollEvents ? { pollEvents: source.pollEvents } : {}),
    ...(source.getChatViewMode ? { getChatViewMode: source.getChatViewMode } : {}),
    getArtifact: source.getArtifact ?? refuse,
    getWorkspaceTree: source.getWorkspaceTree ?? refuse,
    getWorkspaceFile: source.getWorkspaceFile ?? refuse,
    // The chat asks for it as it opens: a source with none has no browser to show.
    getBrowserState: source.getBrowserState ?? (async () => null),
    listSessions: refuse,
    createSession: refuse,
    sendMessage: refuse,
    pauseSession: refuse,
    retrySession: refuse,
    submitAskUserQuestionResponse: refuse,
    uploadWorkspaceFile: refuse,
    deleteWorkspaceFile: refuse,
    acquireBrowserControl: refuse,
    releaseBrowserControl: refuse,
    getWorkspaceDownloadUrl() {
      throw new ReadOnlyChatError();
    },
    browserShellUrl() {
      throw new ReadOnlyChatError();
    },
  };
}
