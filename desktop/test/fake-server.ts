// A device-link server for tests: protocol version 1, as surogates/devices/link.py speaks it.

import { once } from "node:events";
import type { AddressInfo } from "node:net";

import { WebSocketServer, type WebSocket } from "ws";

export interface FakeLinkServerOptions {
  token?: string;
  heartbeatS?: number;
  // false: never send a welcome. Default true.
  welcome?: boolean;
  // false: never answer a ping. Default true.
  pong?: boolean;
}

export class FakeLinkServer {
  readonly received: Record<string, unknown>[] = [];
  readonly hellos: Record<string, unknown>[] = [];
  // The close code of each connection, in order.
  readonly closes: number[] = [];
  connections = 0;
  private server: WebSocketServer | null = null;
  private socket: WebSocket | null = null;

  constructor(private readonly options: FakeLinkServerOptions = {}) {}

  async start(): Promise<string> {
    this.server = new WebSocketServer({ host: "127.0.0.1", port: 0 });
    await once(this.server, "listening");
    this.server.on("connection", (socket, request) => {
      this.connections += 1;
      this.socket = socket;
      socket.on("close", (code) => this.closes.push(code));
      const token = this.options.token ?? "surg_dev_test";
      if (request.headers.authorization !== `Bearer ${token}`) {
        socket.close(4401, "unauthenticated");
        return;
      }
      socket.on("message", (data) => {
        const frame = JSON.parse(data.toString()) as Record<string, unknown>;
        this.received.push(frame);
        if (frame.type === "hello") {
          this.hellos.push(frame);
          if (this.options.welcome !== false) {
            socket.send(JSON.stringify({
              type: "welcome", protocol: 1, device_id: "d", org_id: "o", agent_id: "a",
              user_id: "u", name: "Laptop", heartbeat_s: this.options.heartbeatS ?? 15,
            }));
          }
        } else if (frame.type === "ping" && this.options.pong !== false) {
          socket.send(JSON.stringify({ type: "pong" }));
        }
      });
    });
    const { port } = this.server.address() as AddressInfo;
    return `ws://127.0.0.1:${port}`;
  }

  send(frame: Record<string, unknown> | string): void {
    this.socket?.send(typeof frame === "string" ? frame : JSON.stringify(frame));
  }

  close(code: number): void {
    this.socket?.close(code, "test");
  }

  // Stop reading from the current connection: a half-open link, which never
  // answers a close frame.
  stall(): void {
    this.socket?.pause();
  }

  async until(check: () => boolean, timeoutMs = 3_000): Promise<void> {
    const deadline = Date.now() + timeoutMs;
    while (!check()) {
      if (Date.now() > deadline) throw new Error("timed out waiting");
      await new Promise((resolve) => setTimeout(resolve, 10));
    }
  }

  async stop(): Promise<void> {
    const server = this.server;
    if (server === null) return;
    for (const client of server.clients) client.terminate();
    await new Promise<void>((resolve) => server.close(() => resolve()));
  }
}
