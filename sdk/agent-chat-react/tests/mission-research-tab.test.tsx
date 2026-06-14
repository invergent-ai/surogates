// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// The Research tab renders the Arbor Idea Tree for an `/auto-research`
// mission: nodes in dotted-decimal order (ROOT first), each with status,
// dev score, and a delta vs. the run baseline, plus the dev + held-out
// score headers.

import { act, type ReactElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import { MissionResearchTab } from "../src/components/missions/mission-research-tab";
import type { AgentChatMissionResearch } from "../src/types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

let root: Root | null = null;
let container: HTMLDivElement | null = null;

afterEach(() => {
  if (root) act(() => root?.unmount());
  root = null;
  container?.remove();
  container = null;
});

function mount(node: ReactElement): HTMLDivElement {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => root?.render(node));
  return container;
}

function research(): AgentChatMissionResearch {
  return {
    run: {
      id: "run-1",
      status: "active",
      repoPath: "/workspace/bench",
      trunkBranch: "research/run1/trunk",
      objective: "Improve classification accuracy",
      metricDirection: "maximize",
      baselineScore: 0.5,
      trunkScore: 0.8,
      testBaselineScore: 0.5,
      testTrunkScore: 0.78,
      evalCmd: "python eval.py --split dev",
      evalCmdTest: "python eval.py --split test",
      maxCycles: 20,
      maxParallel: 4,
      mergeThreshold: 0,
    },
    // Deliberately out of order to exercise the dotted-decimal sort.
    nodes: [
      { nodeKey: "2", parentKey: "ROOT", depth: 1, status: "pruned", hypothesis: "bigram scorer", score: 0.8, insight: null, result: null, codeRef: null, taskId: null, createdAt: null, completedAt: null },
      { nodeKey: "ROOT", parentKey: null, depth: 0, status: "pending", hypothesis: "Improve accuracy", score: null, insight: null, result: null, codeRef: null, taskId: null, createdAt: null, completedAt: null },
      { nodeKey: "1", parentKey: "ROOT", depth: 1, status: "merged", hypothesis: "keyword lexicon", score: 0.8, insight: "lexicon + negation wins", result: null, codeRef: null, taskId: null, createdAt: null, completedAt: null },
    ],
  };
}

describe("MissionResearchTab", () => {
  it("renders nodes in dotted-decimal order with scores and held-out header", () => {
    const dom = mount(<MissionResearchTab research={research()} />);

    const keys = Array.from(dom.querySelectorAll("li code")).map((c) =>
      c.textContent?.trim(),
    );
    expect(keys).toEqual(["ROOT", "1", "2"]);

    const text = dom.textContent ?? "";
    expect(text).toContain("Held-out test (authoritative)");
    expect(text).toContain("0.78"); // held-out trunk score
    expect(text).toContain("merged");
    expect(text).toContain("lexicon + negation wins"); // backprop insight
    // dev delta vs baseline 0.5 -> +0.3
    expect(text).toContain("+0.3");
  });

  it("shows an empty-state when the tree has no nodes", () => {
    const empty = research();
    empty.nodes = [];
    const dom = mount(<MissionResearchTab research={empty} />);
    expect(dom.textContent).toContain("No hypotheses yet.");
  });
});
