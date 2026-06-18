import { render } from "@testing-library/react";
import { expect, it, vi } from "vitest";

import { BrowserLiveView } from "../browser-live-view";

const connect = vi.fn();

vi.mock("@novnc/novnc", () => ({
  default: vi.fn().mockImplementation((_el: HTMLElement, url: string) => {
    connect(url);
    return {
      disconnect: vi.fn(),
      viewOnly: false,
      scaleViewport: false,
    };
  }),
}));

it("connects RFB to a wss:// url derived from src", () => {
  render(
    <BrowserLiveView src="https://ops.example/api/sessions/s1/browser/live/?token=t" />,
  );
  expect(connect).toHaveBeenCalledWith(
    "wss://ops.example/api/sessions/s1/browser/live/?token=t",
  );
});

it("renders a canvas container with the rfb test id", () => {
  const { getByTestId } = render(
    <BrowserLiveView src="https://ops.example/api/sessions/s1/browser/live/?token=t" />,
  );
  expect(getByTestId("browser-rfb")).toBeTruthy();
});
