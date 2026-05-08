import { describe, expect, it } from "vitest";
import { request as httpRequest } from "node:http";
import type { AddressInfo } from "node:net";
import { createExampleApp } from "../src/server/app";
import { ExampleSessionStore } from "../src/server/session-store";

describe("ExampleSessionStore workspace", () => {
  it("seeds a workspace tree and reads files", () => {
    const store = new ExampleSessionStore();
    const record = store.create();

    const tree = store.getWorkspaceTree(record);
    const names = tree.entries.map((entry) => entry.name);

    expect(names).toContain("README.md");
    expect(names).toContain("src");
    expect(store.getWorkspaceFile(record, "README.md").content).toContain(
      "Example workspace",
    );
  });

  it("uploads and deletes in-memory files", () => {
    const store = new ExampleSessionStore();
    const record = store.create();

    const upload = store.uploadWorkspaceFile(record, {
      path: "/notes/demo.txt",
      content: "demo",
      mimeType: "text/plain",
    });

    expect(upload).toEqual({ path: "notes/demo.txt", size: 4 });
    expect(store.getWorkspaceFile(record, "notes/demo.txt").content).toBe("demo");

    store.deleteWorkspaceFile(record, "notes/demo.txt");
    expect(() => store.getWorkspaceFile(record, "notes/demo.txt")).toThrow(
      "Workspace file not found",
    );
  });

  it("stores uploaded PDF files as base64 workspace previews", async () => {
    const store = new ExampleSessionStore();
    const record = store.create();
    const pdfBytes = new Uint8Array([0x25, 0x50, 0x44, 0x46, 0x2d, 0xff, 0x0a]);

    await withExampleServer(store, async (baseUrl) => {
      const uploadResponse = await postMultipartFile(
        `${baseUrl}/api/sessions/${record.session.id}/workspace/upload`,
        "report.pdf",
        "application/pdf",
        Buffer.from(pdfBytes),
      );

      expect(uploadResponse.status).toBe(201);

      const file = await getJson(
        `${baseUrl}/api/sessions/${record.session.id}/workspace/file?path=report.pdf`,
      );

      expect(file).toMatchObject({
        path: "report.pdf",
        content: Buffer.from(pdfBytes).toString("base64"),
        size: pdfBytes.byteLength,
        mime_type: "application/pdf",
        encoding: "base64",
        truncated: false,
      });
    });
  });
});

async function withExampleServer(
  store: ExampleSessionStore,
  run: (baseUrl: string) => Promise<void>,
) {
  const app = createExampleApp({ store });
  const server = app.listen(0);
  await new Promise<void>((resolve) => server.once("listening", resolve));
  const address = server.address() as AddressInfo;
  try {
    await run(`http://127.0.0.1:${address.port}`);
  } finally {
    await new Promise<void>((resolve, reject) => {
      server.close((error) => (error ? reject(error) : resolve()));
    });
  }
}

async function postMultipartFile(
  url: string,
  filename: string,
  contentType: string,
  file: Buffer,
) {
  const boundary = "----surogates-test-boundary";
  const body = Buffer.concat([
    Buffer.from(
      `--${boundary}\r\n` +
        `Content-Disposition: form-data; name="file"; filename="${filename}"\r\n` +
        `Content-Type: ${contentType}\r\n\r\n`,
    ),
    file,
    Buffer.from(`\r\n--${boundary}--\r\n`),
  ]);
  return request("POST", url, body, {
    "content-type": `multipart/form-data; boundary=${boundary}`,
    "content-length": String(body.byteLength),
  });
}

async function getJson(url: string): Promise<Record<string, unknown>> {
  const response = await request("GET", url);
  return JSON.parse(response.body.toString("utf8")) as Record<string, unknown>;
}

async function request(
  method: string,
  url: string,
  body?: Buffer,
  headers: Record<string, string> = {},
): Promise<{ status: number; body: Buffer }> {
  return await new Promise((resolve, reject) => {
    const req = httpRequest(url, { method, headers }, (res) => {
      const chunks: Buffer[] = [];
      res.on("data", (chunk: Buffer) => chunks.push(chunk));
      res.on("end", () => {
        resolve({
          status: res.statusCode ?? 0,
          body: Buffer.concat(chunks),
        });
      });
    });
    req.on("error", reject);
    if (body) req.write(body);
    req.end();
  });
}
