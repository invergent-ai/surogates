import { describe, expect, it } from "vitest";
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
});
