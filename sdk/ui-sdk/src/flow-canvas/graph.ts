// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// A flow as plain data, where its nodes sit, and how far a run has walked
// it. The shapes are structural: a server's generated flow type, where every
// absent value is null, passes as it is.
import dagre from "@dagrejs/dagre";

import type { PerformerKind } from "../performer-mark";

/** A node of a flow. An exit's name says where it leads. */
export interface FlowGraphNode {
  key: string;
  kind: string;
  name: string;
  performer?: PerformerKind | null;
}

/** An edge from one node to the next, with the words that choose it. */
export interface FlowGraphEdge {
  source: string;
  target: string;
  label?: string | null;
}

export interface FlowGraph {
  nodes: readonly FlowGraphNode[];
  edges: readonly FlowGraphEdge[];
}

/** Where a node stands in a run: walked, open now, or not reached yet. */
export type FlowTraceState = "done" | "current" | "ahead";

export interface FlowTrace {
  /** Each node's state by key; empty when nothing is traced. */
  nodes: Record<string, FlowTraceState>;
  /** The indexes, in `edges`, of the edges the run walked. */
  walked: ReadonlySet<number>;
}

export const FLOW_NODE_WIDTH = 240;
export const FLOW_NODE_HEIGHT = 72;

/** The top-left corner of each node, ranked top to bottom by dagre. */
export function layoutFlow(
  graph: FlowGraph,
): Record<string, { x: number; y: number }> {
  const g = new dagre.graphlib.Graph();
  g.setDefaultEdgeLabel(() => ({}));
  g.setGraph({ rankdir: "TB", nodesep: 40, ranksep: 80 });
  for (const node of graph.nodes) {
    g.setNode(node.key, { width: FLOW_NODE_WIDTH, height: FLOW_NODE_HEIGHT });
  }
  for (const edge of graph.edges) {
    g.setEdge(edge.source, edge.target);
  }
  dagre.layout(g);
  const at: Record<string, { x: number; y: number }> = {};
  for (const node of graph.nodes) {
    const { x, y } = g.node(node.key);
    at[node.key] = {
      x: x - FLOW_NODE_WIDTH / 2,
      y: y - FLOW_NODE_HEIGHT / 2,
    };
  }
  return at;
}

/**
 * How far a run has walked the flow. `path` is the nodes it went through, in
 * order, and `current` the ones open now. The walk starts at the flow's start
 * node even when `path` leaves it out. For each pair of nodes in a row, the
 * first edge from the earlier to the later is the one walked.
 */
export function traceFlow(
  graph: FlowGraph,
  path: readonly string[],
  current: readonly string[],
): FlowTrace {
  if (path.length === 0 && current.length === 0) {
    return { nodes: {}, walked: new Set() };
  }
  const start = graph.nodes.find((node) => node.kind === "start")?.key;
  const walk =
    start !== undefined && path.length > 0 && path[0] !== start
      ? [start, ...path]
      : path;

  // A key the flow does not hold (another stage's node) marks nothing.
  const keys = new Set(graph.nodes.map((node) => node.key));
  const nodes: Record<string, FlowTraceState> = {};
  for (const key of keys) nodes[key] = "ahead";
  for (const key of walk) {
    if (keys.has(key)) nodes[key] = "done";
  }
  for (const key of current) {
    if (keys.has(key)) nodes[key] = "current";
  }

  const walked = new Set<number>();
  for (let i = 1; i < walk.length; i += 1) {
    const from = walk[i - 1];
    const to = walk[i];
    const edge = graph.edges.findIndex(
      (e) => e.source === from && e.target === to,
    );
    if (edge !== -1) walked.add(edge);
  }
  return { nodes, walked };
}
