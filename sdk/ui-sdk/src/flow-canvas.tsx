// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// A read-only flow, laid out top to bottom: each node by name and kind, each
// edge with its label, and, when a run is traced, the path it took in amber
// and the nodes open now lit. A person pans and zooms; nothing moves,
// connects, selects or deletes. Ported from Studio's skill canvas: its
// layout, its path replay and its node.
//
// The flow is plain data, so a server's flow type passes as it is. The app
// gives the canvas its height.
"use client";

import "@xyflow/react/dist/style.css";

import { Background, Controls, type Edge, ReactFlow } from "@xyflow/react";
import { useTheme } from "next-themes";
import { useMemo } from "react";

import { type FlowNodeType, FlowNodeView } from "./flow-canvas/flow-node";
import { type FlowGraph, layoutFlow, traceFlow } from "./flow-canvas/graph";
import { cn } from "./lib/utils";

export {
  FLOW_NODE_HEIGHT,
  FLOW_NODE_WIDTH,
  type FlowGraph,
  type FlowGraphEdge,
  type FlowGraphNode,
  type FlowTrace,
  type FlowTraceState,
  layoutFlow,
  traceFlow,
} from "./flow-canvas/graph";
export { type FlowNodeData, FlowNodeView } from "./flow-canvas/flow-node";

const NODE_TYPES = { flow: FlowNodeView };

// One empty list for the defaults, so an untraced canvas keeps its trace.
const NONE: readonly string[] = [];

export interface FlowCanvasProps {
  graph: FlowGraph;
  /** The nodes a run went through, in order. */
  path?: readonly string[];
  /** The nodes open now. */
  current?: readonly string[];
  className?: string;
}

export function FlowCanvas({
  graph,
  path = NONE,
  current = NONE,
  className,
}: FlowCanvasProps) {
  const { resolvedTheme } = useTheme();
  const at = useMemo(() => layoutFlow(graph), [graph]);
  const trace = useMemo(
    () => traceFlow(graph, path, current),
    [graph, path, current],
  );

  const nodes = useMemo<FlowNodeType[]>(
    () =>
      graph.nodes.map((node) => ({
        id: node.key,
        type: "flow",
        position: at[node.key],
        data: {
          node,
          state: Object.hasOwn(trace.nodes, node.key)
            ? trace.nodes[node.key]
            : null,
        },
      })),
    [graph, at, trace],
  );

  // A walked edge is drawn in amber, and the one into a node open now runs.
  const edges = useMemo<Edge[]>(
    () =>
      graph.edges.map((edge, i) => ({
        id: `e${i}`,
        source: edge.source,
        target: edge.target,
        label: edge.label || undefined,
        ...(trace.walked.has(i) && {
          animated: trace.nodes[edge.target] === "current",
          style: { stroke: "var(--accent-amber)", strokeWidth: 2 },
        }),
      })),
    [graph, trace],
  );

  return (
    <div data-slot="flow-canvas" className={cn("h-full w-full", className)}>
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={NODE_TYPES}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable={false}
        deleteKeyCode={null}
        // Without a theme provider, follow the system rather than assume light.
        colorMode={
          resolvedTheme === undefined
            ? "system"
            : resolvedTheme === "dark"
              ? "dark"
              : "light"
        }
        fitView
      >
        <Background />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  );
}
