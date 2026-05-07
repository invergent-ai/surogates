import { describe, expect, it, vi } from "vitest";
import { mkdtempSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { loadExampleEnv } from "../src/server/env";

describe("loadExampleEnv", () => {
  it("loads an existing env file through Node's env loader", () => {
    const dir = mkdtempSync(join(tmpdir(), "example-chat-env-"));
    const envPath = join(dir, ".env");
    writeFileSync(envPath, "OPENAI_MODEL=demo-model\n");
    const loadEnvFile = vi.fn();

    const loaded = loadExampleEnv({ envPath, loadEnvFile });

    expect(loaded).toBe(true);
    expect(loadEnvFile).toHaveBeenCalledWith(envPath);
  });

  it("skips missing env files", () => {
    const loadEnvFile = vi.fn();

    const loaded = loadExampleEnv({
      envPath: "/definitely/missing/example-chat-app.env",
      loadEnvFile,
    });

    expect(loaded).toBe(false);
    expect(loadEnvFile).not.toHaveBeenCalled();
  });
});
