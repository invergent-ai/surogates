import { createHmac, randomBytes } from "node:crypto";
import { readFileSync } from "node:fs";
import { createServer } from "node:http";
import { HOST_HTML } from "../../src/host.js";
import type { Office } from "../../src/office.js";

export const IT = {
  dsUrl: process.env.OFFICE_IT_DS_URL ?? "",
  secret: process.env.OFFICE_IT_JWT_SECRET ?? "",
  // How the document server reaches this machine, e.g. http://172.17.32.40:8791.
  host: process.env.OFFICE_IT_HOST ?? "",
  port: Number(process.env.OFFICE_IT_PORT ?? 8791),
};
export const itEnabled = Boolean(IT.dsUrl && IT.secret && IT.host);

export const AGENT = { id: "agent:it:session", name: "Agent" };
export const USER = { id: "user:it", name: "Flavius" };

export const newKey = (): string => randomBytes(16).toString("hex");

export function sign(payload: object): string {
  const part = (value: object) => Buffer.from(JSON.stringify(value)).toString("base64url");
  const unsigned = `${part({ alg: "HS256", typ: "JWT" })}.${part(payload)}`;
  return `${unsigned}.${createHmac("sha256", IT.secret).update(unsigned).digest("base64url")}`;
}

const TYPES: Record<string, string> = { docx: "word", xlsx: "cell", pptx: "slide", pdf: "pdf" };

export function editorConfig(key: string, user: { id: string; name: string }, file = "sample.docx"): Record<string, unknown> {
  const fileType = file.split(".").pop()!;
  const config = {
    width: "100%",
    height: "100%",
    document: { fileType, key, title: file, url: `${IT.host}/files/${file}`, permissions: { edit: true, review: true, comment: true } },
    documentType: TYPES[fileType],
    editorConfig: {
      callbackUrl: `${IT.host}/callback`,
      mode: "edit",
      lang: "en",
      user,
      coEditing: { mode: "fast", change: false },
      customization: { forcesave: true },
    },
  };
  return { ...config, token: sign(config) };
}

export interface Fixture {
  hostUrl: string;
  callbacks: Record<string, unknown>[];
  close(): Promise<void>;
}

/** Serves the fixture documents and the host page, and records the document server's callbacks. */
export async function startFixture(): Promise<Fixture> {
  const callbacks: Record<string, unknown>[] = [];
  const server = createServer((req, res) => {
    if (req.method === "POST" && req.url === "/callback") {
      let body = "";
      req.on("data", (chunk) => { body += chunk; });
      req.on("end", () => {
        callbacks.push(JSON.parse(body) as Record<string, unknown>);
        res.end('{"error":0}');
      });
      return;
    }
    if (req.url === "/host.html") {
      res.setHeader("content-type", "text/html; charset=utf-8");
      res.end(HOST_HTML);
      return;
    }
    const file = /^\/files\/([\w.-]+)$/.exec(req.url ?? "");
    try {
      if (!file) throw new Error("not found");
      // blank.<type> is the document office_edit creates a new one from.
      const path = /^blank\.\w+$/.test(file[1]!) ? `../../../surogates/office/templates/${file[1]}` : `../fixtures/${file[1]}`;
      res.end(readFileSync(new URL(path, import.meta.url)));
    } catch {
      res.statusCode = 404;
      res.end();
    }
  });
  await new Promise<void>((resolve) => server.listen(IT.port, "0.0.0.0", resolve));
  return {
    hostUrl: `http://127.0.0.1:${IT.port}/host.html`,
    callbacks,
    close: () => new Promise<void>((resolve) => server.close(() => resolve())),
  };
}

/** An agent page and a person page on one fresh key, their ids added to *opened* for the test to close. */
export async function pair(office: Office, opened: string[], file = "sample.docx"): Promise<{ agent: string; user: string }> {
  const key = newKey();
  const agent = await office.open({ docKey: key, sessionId: "agent", config: editorConfig(key, AGENT, file), documentServerUrl: IT.dsUrl });
  const user = await office.open({ docKey: key, sessionId: "user", config: editorConfig(key, USER, file), documentServerUrl: IT.dsUrl });
  opened.push(agent, user);
  return { agent, user };
}

/** Runs *check* until it passes or *ms* run out: the other editor can still be taking in a change. */
export async function eventually(check: () => Promise<void>, ms = 10_000): Promise<void> {
  const until = Date.now() + ms;
  for (;;) {
    try {
      return await check();
    } catch (error) {
      if (Date.now() > until) throw error;
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
  }
}
