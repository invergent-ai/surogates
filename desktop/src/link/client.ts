// One authenticated WebSocket to the agent, kept up while the app runs: the
// device link (surogates/devices/link.py). It reconnects with backoff, and
// stops for good when the server says the token is unknown, the device was
// revoked, another connection took over, or this app is too old.

import WebSocket from "ws";

import { reconnectDelayMs } from "./backoff.js";
import {
  Close,
  hello,
  MAX_FRAME_CHARS,
  type Operation,
  parseServerFrame,
  ProtocolError,
  type ServerFrame,
  WELCOME_TIMEOUT_MS,
  type Welcome,
} from "./protocol.js";

export type LinkStatus =
  | "connecting"
  | "connected"
  | "offline"
  | "unauthenticated"
  | "revoked"
  | "superseded"
  | "update_required"
  | "stopped";

export interface LinkHandlers {
  onWelcome?(welcome: Welcome): void;
  onOperation(operation: Operation): void;
  onCancel(id: string): void;
  onAck(id: string): void;
  onStatus?(status: LinkStatus): void;
}

export interface DeviceLinkOptions {
  url: string;
  token: string;
  handlers: LinkHandlers;
  // What the hello reports as held unfinished, read at each connect.
  openIds: () => string[];
  welcomeTimeoutMs?: number;
  delay?: (attempt: number) => number;
}

// Closes after which the app must not reconnect.
const FINAL: Partial<Record<number, LinkStatus>> = {
  [Close.unauthenticated]: "unauthenticated",
  [Close.revoked]: "revoked",
  [Close.superseded]: "superseded",
};

export class DeviceLink {
  status: LinkStatus = "offline";
  private socket: WebSocket | null = null;
  private welcomed = false;
  private stopped = false;
  private attempt = 0;
  private retry: NodeJS.Timeout | null = null;

  constructor(private readonly options: DeviceLinkOptions) {}

  start(): void {
    if (this.socket !== null || this.retry !== null) return;
    this.stopped = false;
    this.connect();
  }

  async stop(): Promise<void> {
    this.stopped = true;
    if (this.retry !== null) clearTimeout(this.retry);
    // A cleared timer must not leave start() thinking a retry is pending.
    this.retry = null;
    const socket = this.socket;
    if (socket !== null && socket.readyState !== WebSocket.CLOSED) {
      await new Promise<void>((resolve) => {
        socket.once("close", () => resolve());
        socket.close(1000, "app stopped");
      });
    }
    this.setStatus("stopped");
  }

  /** Send a frame on the welcomed connection; false when there is none. */
  send(frame: Record<string, unknown>): boolean {
    const socket = this.socket;
    if (socket === null || socket.readyState !== WebSocket.OPEN || !this.welcomed) return false;
    socket.send(JSON.stringify(frame));
    return true;
  }

  private setStatus(status: LinkStatus): void {
    this.status = status;
    this.options.handlers.onStatus?.(status);
  }

  private connect(): void {
    this.setStatus("connecting");
    const socket = new WebSocket(this.options.url, {
      headers: { Authorization: `Bearer ${this.options.token}` },
      maxPayload: 2 * MAX_FRAME_CHARS,
    });
    this.socket = socket;
    this.welcomed = false;
    let lastFrameAt = Date.now();
    let pinger: NodeJS.Timeout | null = null;
    let final: LinkStatus | undefined;
    const welcomeTimer = setTimeout(() => socket.terminate(), this.options.welcomeTimeoutMs ?? WELCOME_TIMEOUT_MS);

    socket.on("open", () => {
      socket.send(JSON.stringify(hello(this.options.openIds())));
    });

    socket.on("message", (data, isBinary) => {
      // Any frame counts as liveness, not only a pong.
      lastFrameAt = Date.now();
      if (isBinary) {
        socket.close(Close.protocol, "text frames only");
        return;
      }
      let frame: ServerFrame;
      try {
        frame = parseServerFrame(data.toString());
      } catch (error) {
        socket.close(Close.protocol, error instanceof ProtocolError ? error.message : "malformed frame");
        return;
      }
      switch (frame.type) {
        case "welcome": {
          // One welcome per connection: a repeat would start a second pinger.
          if (this.welcomed) break;
          clearTimeout(welcomeTimer);
          this.welcomed = true;
          const heartbeatMs = frame.welcome.heartbeatS * 1000;
          pinger = setInterval(() => {
            // No frame within two heartbeats of a ping: the server is gone.
            if (Date.now() - lastFrameAt > 2 * heartbeatMs) {
              socket.terminate();
              return;
            }
            socket.send(JSON.stringify({ type: "ping" }));
          }, heartbeatMs);
          this.setStatus("connected");
          this.options.handlers.onWelcome?.(frame.welcome);
          break;
        }
        case "pong":
          // A link that answers a ping is healthy: start the backoff over. Not at
          // the welcome: a link the server closes right after it would loop fast.
          this.attempt = 0;
          break;
        case "unknown":
          break;
        case "op":
          this.options.handlers.onOperation(frame.operation);
          break;
        case "op_ack":
          this.options.handlers.onAck(frame.id);
          break;
        case "cancel":
          this.options.handlers.onCancel(frame.id);
          break;
        case "error":
          if (frame.code === "unsupported_protocol") final = "update_required";
          break;
      }
    });

    // A close always follows an error; the close decides what happens next.
    socket.on("error", () => {});

    socket.on("close", (code) => {
      clearTimeout(welcomeTimer);
      if (pinger !== null) clearInterval(pinger);
      if (this.socket === socket) this.socket = null;
      this.welcomed = false;
      if (this.stopped) return;
      const ending = final ?? FINAL[code];
      if (ending !== undefined) {
        this.stopped = true;
        this.setStatus(ending);
        return;
      }
      this.setStatus("offline");
      const delay = (this.options.delay ?? reconnectDelayMs)(this.attempt++);
      this.retry = setTimeout(() => {
        this.retry = null;
        this.connect();
      }, delay);
    });
  }
}
