import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";

import { BrowserPane } from "../browser-pane";
import type { AgentChatBrowserState } from "../../../types";

// noVNC is browser-only and would open a real WebSocket; stub it so mounting
// BrowserLiveView is a no-op in the test.
vi.mock("@novnc/novnc", () => ({
  default: vi.fn().mockImplementation(() => ({
    disconnect: vi.fn(),
    viewOnly: false,
    scaleViewport: false,
  })),
}));

function makeAdapter() {
  return {
    browserLiveViewUrl: (sessionId: string) =>
      `https://ops.example/api/sessions/${sessionId}/browser/live/?token=t`,
    getBrowserPreviewSnapshot: vi.fn().mockResolvedValue(null),
    acquireBrowserControl: vi.fn().mockResolvedValue(undefined),
    releaseBrowserControl: vi.fn().mockResolvedValue(undefined),
  };
}

const liveState: AgentChatBrowserState = { status: "live", controlOwner: null };

it("does not mount the RFB live view without user control", () => {
  render(<BrowserPane sessionId="s1" state={liveState} adapter={makeAdapter()} />);
  expect(screen.queryByTestId("browser-rfb")).toBeNull();
});

it("mounts the RFB live view once the user takes control", async () => {
  render(<BrowserPane sessionId="s1" state={liveState} adapter={makeAdapter()} />);
  expect(screen.queryByTestId("browser-rfb")).toBeNull();

  fireEvent.click(screen.getByRole("button", { name: /take control/i }));

  expect(await screen.findByTestId("browser-rfb")).toBeTruthy();
});
