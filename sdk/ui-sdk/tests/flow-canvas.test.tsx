// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// A read-only flow: laid out top to bottom, each node by name and kind, each
// edge with its label, the path taken and the open nodes marked, and nothing
// a person drags, connects or deletes changes it.
import { act } from "react";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import {
  FlowCanvas,
  type FlowGraph,
  layoutFlow,
  traceFlow,
} from "../src/flow-canvas";
import { cleanup, click, render, settle } from "./dom";
import { standInForReactFlow } from "./react-flow-dom";

beforeAll(standInForReactFlow);
afterEach(cleanup);

const graph: FlowGraph = {
  nodes: [
    { key: "#start", kind: "start", name: "Start" },
    {
      key: "check",
      kind: "step",
      name: "Check documents",
      performer: "person",
    },
    {
      key: "review",
      kind: "step",
      name: "Review",
      performer: "agent_with_person",
    },
    { key: "decide", kind: "decision", name: "Route by risk" },
    { key: "close:approved", kind: "exit", name: "Closed: approved" },
  ],
  edges: [
    { source: "#start", target: "check", label: null },
    { source: "check", target: "review", label: "Complete" },
    { source: "review", target: "check", label: "Send back" },
    { source: "review", target: "decide", label: "Approve" },
    { source: "decide", target: "close:approved", label: "Low risk" },
  ],
};

/** Draws the flow and waits for React Flow to measure it. */
async function canvas(
  props: { path?: string[]; current?: string[] } = {},
): Promise<HTMLElement> {
  const view = render(
    <div style={{ width: "800px", height: "600px" }}>
      <FlowCanvas graph={graph} {...props} />
    </div>,
  );
  await settle();
  return view;
}

function nodeOf(view: HTMLElement, key: string): HTMLElement {
  const node = Array.from(
    view.querySelectorAll<HTMLElement>(".react-flow__node"),
  ).find((candidate) => candidate.getAttribute("data-id") === key);
  if (!node) throw new Error(`no node "${key}"`);
  return node;
}

/** The node's own box, which carries its trace state. */
function boxOf(view: HTMLElement, key: string): HTMLElement {
  const box = nodeOf(view, key).querySelector<HTMLElement>(
    "[data-slot=flow-node]",
  );
  if (!box) throw new Error(`node "${key}" draws no box`);
  return box;
}

function mouse(
  target: EventTarget,
  type: "mousedown" | "mousemove" | "mouseup",
  at: { x: number; y: number },
): void {
  act(() => {
    target.dispatchEvent(
      new MouseEvent(type, {
        bubbles: true,
        cancelable: true,
        view: window,
        button: 0,
        buttons: type === "mouseup" ? 0 : 1,
        clientX: at.x,
        clientY: at.y,
      }),
    );
  });
}

function press(target: EventTarget, key: string): void {
  act(() => {
    target.dispatchEvent(
      new KeyboardEvent("keydown", { bubbles: true, cancelable: true, key }),
    );
  });
}

function expectWhole(view: HTMLElement): void {
  expect(view.querySelectorAll(".react-flow__node")).toHaveLength(5);
  expect(view.querySelectorAll(".react-flow__edge")).toHaveLength(5);
}

describe("flow canvas", () => {
  it("the flow lays out top to bottom", () => {
    const at = layoutFlow(graph);
    const ys = ["#start", "check", "review", "decide", "close:approved"].map(
      (key) => at[key].y,
    );

    for (let i = 1; i < ys.length; i += 1) {
      expect(ys[i]).toBeGreaterThan(ys[i - 1]);
    }
  });

  it("the flow shows each node by name and kind, an exit by where it leads, and each labelled edge", async () => {
    const view = await canvas();
    const text = view.textContent ?? "";

    expect(view.querySelectorAll(".react-flow__node")).toHaveLength(5);
    for (const words of [
      "Check documents",
      "Route by risk",
      "Decision",
      "Closed: approved",
      "Exit",
    ]) {
      expect(text).toContain(words);
    }
    expect(
      nodeOf(view, "review").querySelector(
        '[role=img][aria-label="Agent, finished by a person"]',
      ),
    ).not.toBeNull();

    expect(view.querySelectorAll(".react-flow__edge")).toHaveLength(5);
    for (const label of ["Complete", "Send back", "Approve", "Low risk"]) {
      expect(text).toContain(label);
    }
  });

  it("the trace marks the path done, the open nodes current and the rest ahead", async () => {
    expect(
      traceFlow(graph, ["check", "review", "check", "review"], ["review"]),
    ).toEqual({
      nodes: {
        "#start": "done",
        check: "done",
        review: "current",
        decide: "ahead",
        "close:approved": "ahead",
      },
      walked: new Set([0, 1, 2]),
    });
    expect(traceFlow(graph, [], [])).toEqual({ nodes: {}, walked: new Set() });

    const traced = await canvas({
      path: ["check", "review", "check", "review"],
      current: ["review"],
    });
    expect(boxOf(traced, "check").getAttribute("data-state")).toBe("done");
    expect(boxOf(traced, "review").getAttribute("data-state")).toBe("current");
    expect(boxOf(traced, "decide").getAttribute("data-state")).toBe("ahead");
    // The walked edges are amber, and the one into the open node runs.
    const edges = Array.from(
      traced.querySelectorAll<SVGGElement>(".react-flow__edge"),
    );
    expect(
      edges
        .filter(
          (edge) =>
            edge.querySelector<SVGPathElement>(".react-flow__edge-path")?.style
              .stroke === "var(--accent-amber)",
        )
        .map((edge) => edge.getAttribute("data-id")),
    ).toEqual(["e0", "e1", "e2"]);
    expect(
      edges
        .filter((edge) => edge.classList.contains("animated"))
        .map((edge) => edge.getAttribute("data-id")),
    ).toEqual(["e1"]);
    cleanup();

    const untraced = await canvas();
    expect(untraced.querySelectorAll(".react-flow__node")).toHaveLength(5);
    expect(
      untraced.querySelectorAll(".react-flow__node [data-state]"),
    ).toHaveLength(0);
  });

  it("a drag or a connect attempt changes nothing", async () => {
    const view = await canvas();

    // A drag starts on the first move and carries the node from the next, so
    // each attempt moves twice.
    const review = nodeOf(view, "review");
    const before = review.style.transform;
    mouse(review, "mousedown", { x: 100, y: 100 });
    mouse(window, "mousemove", { x: 200, y: 200 });
    mouse(window, "mousemove", { x: 300, y: 300 });
    mouse(window, "mouseup", { x: 300, y: 300 });
    await settle();
    expect(nodeOf(view, "review").style.transform).toBe(before);
    expectWhole(view);

    const from = nodeOf(view, "check").querySelector(
      ".react-flow__handle.source",
    );
    const to = nodeOf(view, "decide").querySelector(
      ".react-flow__handle.target",
    );
    if (!from || !to) throw new Error("a node draws no handle");
    // The page lays nothing out, so nothing is under the pointer until the
    // test says what is: decide's target handle, where the attempt ends.
    const under = vi.spyOn(document, "elementFromPoint").mockReturnValue(to);
    mouse(from, "mousedown", { x: 100, y: 100 });
    mouse(to, "mousemove", { x: 120, y: 180 });
    mouse(to, "mousemove", { x: 150, y: 250 });
    mouse(to, "mouseup", { x: 150, y: 250 });
    under.mockRestore();
    await settle();
    expectWhole(view);

    const decide = nodeOf(view, "decide");
    click(decide);
    press(decide, "Backspace");
    press(decide, "Delete");
    await settle();
    expectWhole(view);
  });
});
