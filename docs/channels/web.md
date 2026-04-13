# Web Channel

The web chat UI is a browser-based interface designed as a terminal-like experience, not a typical chatbot bubble interface. It talks directly to the REST API -- no separate adapter process needed.

## Features

| Feature | Description |
|---|---|
| **Real-time streaming** | LLM responses stream token-by-token as they are generated. Tool calls, results, and thinking blocks appear live. |
| **Session management** | Create new sessions, switch between active/paused/completed sessions, and browse session history from the sidebar. |
| **Tool visibility** | Every tool call is shown with its name, arguments, execution status, output, and duration. Collapsible for long output. |
| **Thinking blocks** | LLM reasoning is rendered in a distinct style so you can follow the agent's thought process. |
| **Expert delegations** | When the agent delegates to an expert, the expert's mini-loop is shown as a nested interaction. |
| **File uploads** | Attach images, documents, and other files to your messages. |
| **Workspace browser** | Browse and view files in the session's workspace. |
| **Auto-reconnect** | If the connection drops, the UI reconnects and replays missed events automatically. No data loss. |

## Usage

1. Open the web UI in your browser (served at the root path `/` by the API server, or `http://localhost:5173` during development).
2. Log in with your credentials (email and password).
3. Click **New Session** to start a conversation.
4. Type a message and press **Enter** to send (Shift+Enter for newline).
5. Watch the agent work: responses stream in real time, tool calls show progress, and results appear inline.
6. Switch between sessions using the sidebar. Sessions persist across browser refreshes.

## Authentication

The UI handles JWT authentication automatically:
- On first visit, you are prompted to log in.
- Access tokens are short-lived (30 minutes) and refresh automatically in the background.
- If a token expires or becomes invalid, you are redirected to the login screen.
