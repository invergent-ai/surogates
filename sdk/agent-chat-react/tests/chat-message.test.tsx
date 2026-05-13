import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ChatMessage } from "../src/components/chat/chat-message";
import type { AgentChatMessage } from "../src/types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

let root: Root | null = null;
let container: HTMLDivElement | null = null;

afterEach(() => {
  if (root) {
    act(() => root?.unmount());
  }
  root = null;
  container?.remove();
  container = null;
});

function render(node: React.ReactNode): HTMLDivElement {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    root?.render(node);
  });
  return container;
}

function userMessage(overrides: Partial<AgentChatMessage> = {}): AgentChatMessage {
  return {
    id: "evt-1",
    role: "user",
    content: "summarize",
    createdAt: new Date("2026-01-01T00:00:00Z"),
    status: "complete",
    ...overrides,
  };
}

describe("ChatMessage — user bubble attachment chips", () => {
  it("renders one chip per attachment with filename and size", () => {
    const node = render(
      <ChatMessage
        message={userMessage({
          attachments: [
            {
              path: "uploads/1-0-report.pdf",
              filename: "report.pdf",
              mimeType: "application/pdf",
              size: 12_300_000,
            },
            {
              path: "uploads/1-1-notes.txt",
              filename: "notes.txt",
              mimeType: "text/plain",
              size: 4200,
            },
          ],
        })}
        isLast
      />,
    );

    const chips = node.querySelectorAll("button");
    expect(chips).toHaveLength(2);
    expect(chips[0]?.textContent).toContain("report.pdf");
    expect(chips[0]?.textContent).toContain("12.3 MB");
    expect(chips[1]?.textContent).toContain("notes.txt");
    expect(chips[1]?.textContent).toContain("4.2 KB");
  });

  it("renders the chip enabled when path is present and an onFileSelect handler is given", () => {
    const onFileSelect = vi.fn();
    const node = render(
      <ChatMessage
        message={userMessage({
          attachments: [
            {
              path: "uploads/1-0-report.pdf",
              filename: "report.pdf",
              mimeType: "application/pdf",
              size: 12345,
            },
          ],
        })}
        isLast
        onFileSelect={onFileSelect}
      />,
    );

    const chip = node.querySelector("button");
    expect(chip).not.toBeNull();
    expect(chip?.hasAttribute("disabled")).toBe(false);

    act(() => {
      chip?.click();
    });
    expect(onFileSelect).toHaveBeenCalledWith("uploads/1-0-report.pdf");
  });

  it("renders the chip disabled when path is undefined (optimistic state)", () => {
    const onFileSelect = vi.fn();
    const node = render(
      <ChatMessage
        message={userMessage({
          id: "local-1",
          attachments: [
            {
              filename: "report.pdf",
              mimeType: "application/pdf",
              size: 12345,
              // path absent — optimistic chip
            },
          ],
        })}
        isLast
        onFileSelect={onFileSelect}
      />,
    );

    const chip = node.querySelector("button");
    expect(chip?.hasAttribute("disabled")).toBe(true);

    act(() => {
      chip?.click();
    });
    expect(onFileSelect).not.toHaveBeenCalled();
  });

  it("renders the chip disabled when onFileSelect is omitted", () => {
    const node = render(
      <ChatMessage
        message={userMessage({
          attachments: [
            {
              path: "uploads/x.pdf",
              filename: "x.pdf",
              mimeType: "application/pdf",
              size: 1,
            },
          ],
        })}
        isLast
      />,
    );

    const chip = node.querySelector("button");
    expect(chip?.hasAttribute("disabled")).toBe(true);
  });

  it("omits the size span when size is not provided", () => {
    const node = render(
      <ChatMessage
        message={userMessage({
          attachments: [
            {
              path: "uploads/x.bin",
              filename: "x.bin",
              mimeType: "application/octet-stream",
            },
          ],
        })}
        isLast
      />,
    );

    const text = node.textContent ?? "";
    expect(text).toContain("x.bin");
    expect(text).not.toMatch(/(KB|MB|GB|\bB\b)/);
  });

  it("does not render an attachment strip when attachments is undefined or empty", () => {
    const node = render(
      <ChatMessage
        message={userMessage({ attachments: undefined })}
        isLast
      />,
    );
    expect(node.querySelector("button")).toBeNull();

    act(() => {
      root?.render(
        <ChatMessage
          message={userMessage({ attachments: [] })}
          isLast
        />,
      );
    });
    expect(node.querySelector("button")).toBeNull();
  });

  it("renders both images and attachments side-by-side on the same user message", () => {
    const node = render(
      <ChatMessage
        message={userMessage({
          images: [
            { data: "data:image/png;base64,xxxx", mimeType: "image/png" },
          ],
          attachments: [
            {
              path: "uploads/notes.txt",
              filename: "notes.txt",
              mimeType: "text/plain",
              size: 42,
            },
          ],
        })}
        isLast
      />,
    );

    expect(node.querySelectorAll("img")).toHaveLength(1);
    expect(node.querySelectorAll("button")).toHaveLength(1);
  });
});
