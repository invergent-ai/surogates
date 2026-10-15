import { timingSafeEqual } from "node:crypto";
import { createServer as createHttpServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import { HOST_HTML } from "./host.js";
import { ContentError } from "./markdown.js";
import { EditorError, type Office } from "./office.js";
import { Busy, PageGone } from "./pages.js";

export type OfficeApi = Pick<Office, "open" | "read" | "edit" | "flush" | "close" | "health">;

const MAX_BODY = 20 * 1024 * 1024;

class HttpError extends Error {
  constructor(readonly status: number, message: string) {
    super(message);
  }
}

export function createServer(office: OfficeApi, options: { token: string; maxBody?: number }): Server {
  const expected = Buffer.from(`Bearer ${options.token}`);
  const maxBody = options.maxBody ?? MAX_BODY;
  return createHttpServer((req, res) => {
    handle(req, res).catch((error: unknown) => {
      const [status, message] =
        error instanceof HttpError ? [error.status, error.message]
        : error instanceof ContentError ? [400, error.message]
        : error instanceof Busy ? [503, error.message]
        : error instanceof PageGone ? [410, error.message]
        : error instanceof EditorError ? [502, error.message]
        : [500, "internal error"];
      if (status === 500) console.error(error);
      send(res, status, { error: message });
    });
  });

  async function handle(req: IncomingMessage, res: ServerResponse): Promise<void> {
    const url = new URL(req.url ?? "/", "http://runtime-office");
    if (req.method === "GET" && url.pathname === "/health") return send(res, 200, office.health());
    if (req.method === "GET" && url.pathname === "/host.html") {
      res.writeHead(200, { "content-type": "text/html; charset=utf-8" }).end(HOST_HTML);
      return;
    }
    const given = Buffer.from(req.headers.authorization ?? "");
    if (given.length !== expected.length || !timingSafeEqual(given, expected)) throw new HttpError(401, "unauthorized");
    if (req.method === "POST" && url.pathname === "/pages") {
      const body = await readJson(req, maxBody);
      const id = await office.open({
        docKey: required(body.doc_key, "doc_key"),
        sessionId: required(body.session_id, "session_id"),
        config: object(body.config, "config"),
        documentServerUrl: required(body.document_server_url, "document_server_url"),
      });
      return send(res, 200, { page_id: id });
    }
    const route = /^\/pages\/([^/]+)(?:\/(read|edit|flush))?$/.exec(url.pathname);
    if (!route) throw new HttpError(404, "not found");
    const id = decodeURIComponent(route[1]!);
    const action = route[2];
    if (req.method === "DELETE" && !action) {
      await office.close(id);
      res.writeHead(204).end();
      return;
    }
    if (req.method !== "POST" || !action) throw new HttpError(405, "method not allowed");
    const body = await readJson(req, maxBody);
    if (action === "read") return send(res, 200, await office.read(id, body));
    if (action === "edit") return send(res, 200, await office.edit(id, body.ops));
    return send(res, 200, { flushed: await office.flush(id) });
  }
}

function required(value: unknown, name: string): string {
  if (typeof value !== "string" || !value) throw new HttpError(400, `"${name}" is required`);
  return value;
}

function object(value: unknown, name: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) throw new HttpError(400, `"${name}" is an object`);
  return value as Record<string, unknown>;
}

async function readJson(req: IncomingMessage, maxBody: number): Promise<Record<string, unknown>> {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of req) {
    size += (chunk as Buffer).length;
    // Read to the end even past the limit: a client that sends its whole body before reading the answer (httpx)
    // would otherwise never see the 413.
    if (size <= maxBody) chunks.push(chunk as Buffer);
  }
  if (size > maxBody) throw new HttpError(413, "the request is too large");
  try {
    const value: unknown = JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}");
    return object(value, "the body");
  } catch (error) {
    if (error instanceof HttpError) throw error;
    throw new HttpError(400, "the body is not JSON");
  }
}

function send(res: ServerResponse, status: number, body: unknown): void {
  if (res.headersSent) return;
  res.writeHead(status, { "content-type": "application/json" }).end(JSON.stringify(body));
}
