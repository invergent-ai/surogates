// One authenticated WebSocket to the agent, kept up while the app runs: the
// device link (surogates/devices/link.py). It reconnects with backoff, and
// stops for good when the server says the token is unknown, the device was
// revoked, another connection took over, or this app is too old.

import WebSocket from "ws";

import { report } from "../report.js";
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
  // A handler above threw: what it keeps is broken, so the link stops (it is not
  // restarted) after this is told.
  onError?(error: unknown): void;
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

// How long stop() waits for the server to answer the close before it cuts the
// link: ws would wait 30 s on a half-open one, and the app is quitting.
export const CLOSE_TIMEOUT_MS = 2_000;
// Interval ticks are a millisecond or so off, and a check that lands just
// short of the deadline would drop a silent server a whole heartbeat late.
const TICK_SLACK_MS = 50;

// What the server ended the link with: a later stop() leaves it showing.
const ENDED: readonly LinkStatus[] = ["unauthenticated", "revoked", "superseded", "update_required"];

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
        const cut = setTimeout(() => socket.terminate(), CLOSE_TIMEOUT_MS);
        socket.once("close", () => {
          clearTimeout(cut);
          resolve();
        });
        socket.close(1000, "app stopped");
      });
    }
    if (!ENDED.includes(this.status)) this.setStatus("stopped");
  }

  /** Send a frame on the welcomed connection; false when there is none. */
  send(frame: Record<string, unknown>): boolean {
    const socket = this.socket;
    if (socket === null || socket.readyState !== WebSocket.OPEN || !this.welcomed) return false;
    socket.send(JSON.stringify(frame));
    return true;
  }

  // Status is display only: a callback that throws is reported, and the link goes on.
  private setStatus(status: LinkStatus): void {
    this.status = status;
    try {
      this.options.handlers.onStatus?.(status);
    } catch (error) {
      report(this.options.handlers.onError, error);
    }
  }

  private connect(): void {
    this.setStatus("connecting");
    const socket = new WebSocket(this.options.url, {
      headers: { Authorization: `Bearer ${this.options.token}` },
      maxPayload: 2 * MAX_FRAME_CHARS,
    });
    this.socket = socket;
    this.welcomed = false;
    // When the oldest ping that no frame has followed went out.
    let pingedAt: number | null = null;
    let pinger: NodeJS.Timeout | null = null;
    let final: LinkStatus | undefined;
    const welcomeTimer = setTimeout(() => socket.terminate(), this.options.welcomeTimeoutMs ?? WELCOME_TIMEOUT_MS);

    socket.on("open", () => {
      let open: string[];
      try {
        open = this.options.openIds();
      } catch (error) {
        // What it reads is broken: no hello can say what is held, so the link stops.
        report(this.options.handlers.onError, error);
        void this.stop();
        return;
      }
      socket.send(JSON.stringify(hello(open)));
    });

    socket.on("message", (data, isBinary) => {
      // ws keeps reading while the link closes: what it already read behind a stop
      // (a handler that threw, a refused device) is not handed on.
      if (this.stopped) return;
      // Any frame counts as liveness, not only a pong.
      pingedAt = null;
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
      // A handler that throws would reach ws as an uncaught exception.
      try {
        switch (frame.type) {
          case "welcome": {
            // One welcome per connection: a repeat would start a second pinger.
            if (this.welcomed) break;
            clearTimeout(welcomeTimer);
            this.welcomed = true;
            const heartbeatMs = frame.welcome.heartbeatS * 1000;
            pinger = setInterval(() => {
              const now = Date.now();
              // No frame within two heartbeats of a ping: the server is gone.
              if (pingedAt !== null && now - pingedAt >= 2 * heartbeatMs - TICK_SLACK_MS) {
                socket.terminate();
                return;
              }
              socket.send(JSON.stringify({ type: "ping" }));
              pingedAt ??= now;
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
      } catch (error) {
        report(this.options.handlers.onError, error);
        void this.stop();
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
