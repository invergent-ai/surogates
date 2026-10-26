// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// A read-only flow, laid out top to bottom: each node by name and kind, each
// edge with its label, and, when a run is traced, the path it took in amber
// and the nodes open now lit. A node may carry a mark, such as the problems
// found at it. A person pans and zooms, and, when the app gives `onSelect`,
// picks a node by click or keyboard; nothing moves, connects or deletes. An
// app may put a + on lines, with `onInsert`, to insert there.
// Ported from Studio's skill canvas: its layout, its path replay and its
// node.
//
// The flow is plain data, so a server's flow type passes as it is. The app
// gives the canvas its height.
"use client";

import "@xyflow/react/dist/style.css";

import { Background, Controls, type Edge, ReactFlow } from "@xyflow/react";
import { useTheme } from "next-themes";
import { type KeyboardEvent, useMemo } from "react";

import {
  type FlowNodeMark,
  type FlowNodeType,
  FlowNodeView,
} from "./flow-canvas/flow-node";
import {
  type FlowGraph,
  type FlowGraphEdge,
  type FlowGraphNode,
  layoutFlow,
  traceFlow,
} from "./flow-canvas/graph";
import { InsertEdge, type InsertEdgeType } from "./flow-canvas/insert-edge";
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
export {
  type FlowNodeData,
  type FlowNodeMark,
  FlowNodeView,
} from "./flow-canvas/flow-node";

const NODE_TYPES = { flow: FlowNodeView };
const EDGE_TYPES = { insert: InsertEdge };

// One empty list and one empty set of marks for the defaults, so a canvas
// given neither keeps its trace and its nodes from one render to the next.
const NONE: readonly string[] = [];
const NO_MARKS: Readonly<Record<string, FlowNodeMark>> = {};

export interface FlowCanvasProps {
  graph: FlowGraph;
  /** The nodes a run went through, in order. */
  path?: readonly string[];
  /** The nodes open now. */
  current?: readonly string[];
  /** The key of the node shown as picked; its ring shows even without `onSelect`. */
  selected?: string | null;
  /**
   * Given, a person picks a node by a click, or by Enter or Space on it, and
   * lets it go by a click on the pane or, when one is picked, by Escape.
   */
  onSelect?: (key: string | null) => void;
  /** The marks on nodes, by node key. */
  marks?: Readonly<Record<string, FlowNodeMark>>;
  /**
   * Given, each line `insertable` allows carries a + at its middle, after its
   * label. A click on it, or Enter or Space while it has the focus, gives
   * that edge of `graph.edges`. Pass a stable callback: a new one rebuilds
   * every line.
   */
  onInsert?: (edge: FlowGraphEdge) => void;
  /**
   * Which lines carry a +, when `onInsert` is given; every line when left
   * out. Pass a stable callback: a new one rebuilds every line.
   */
  insertable?: (edge: FlowGraphEdge) => boolean;
  className?: string;
}

/** Whether a person may pick the node, on a canvas that picks. */
function pickable(node: FlowGraphNode): boolean {
  return node.pickable !== false;
}

export function FlowCanvas({
  graph,
  path = NONE,
  current = NONE,
  selected = null,
  onSelect,
  marks = NO_MARKS,
  onInsert,
  insertable,
  className,
}: FlowCanvasProps) {
  const { resolvedTheme } = useTheme();
  const at = useMemo(() => layoutFlow(graph), [graph]);
  const trace = useMemo(
    () => traceFlow(graph, path, current),
    [graph, path, current],
  );

  const picking = onSelect !== undefined;
  const nodes = useMemo<FlowNodeType[]>(
    () =>
      graph.nodes.map((node) => {
        const mark = Object.hasOwn(marks, node.key) ? marks[node.key] : null;
        return {
          id: node.key,
          type: "flow",
          position: at[node.key],
          data: {
            node,
            state: Object.hasOwn(trace.nodes, node.key)
              ? trace.nodes[node.key]
              : null,
            selected: node.key === selected,
            mark,
          },
          // A button names itself, so its mark joins its name.
          ...(picking &&
            pickable(node) && {
              className: "cursor-pointer",
              ariaRole: "button" as const,
              ariaLabel: mark ? `${node.name}, ${mark.label}` : node.name,
              domAttributes: { "aria-pressed": node.key === selected },
            }),
        };
      }),
    [graph, at, trace, selected, marks, picking],
  );

  // A walked edge is drawn in amber, and the one into a node open now runs.
  // On a canvas that inserts, every line draws its own label, and a line
  // `insertable` allows carries a + after it.
  const edges = useMemo<(Edge | InsertEdgeType)[]>(() => {
    const names = new Map(graph.nodes.map((node) => [node.key, node.name]));
    return graph.edges.map((edge, i) => ({
      id: `e${i}`,
      source: edge.source,
      target: edge.target,
      ...(onInsert
        ? {
            type: "insert" as const,
            data: {
              label: edge.label || null,
              sourceName: names.get(edge.source) ?? edge.source,
              targetName: names.get(edge.target) ?? edge.target,
              onInsert:
                insertable && !insertable(edge) ? null : () => onInsert(edge),
            },
          }
        : { label: edge.label || undefined }),
      ...(trace.walked.has(i) && {
        animated: trace.nodes[edge.target] === "current",
        style: { stroke: "var(--accent-amber)", strokeWidth: 2 },
      }),
    }));
  }, [graph, trace, onInsert, insertable]);

  // React Flow picks nodes by keyboard only when they are selectable, which
  // would also let them be deleted, so the canvas listens itself.
  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (!onSelect) return;
    if (event.key === "Escape") {
      if (selected !== null) onSelect(null);
      return;
    }
    if (event.key !== "Enter" && event.key !== " ") return;
    const target = event.target as Element;
    if (!target.classList.contains("react-flow__node")) return;
    const key = target.getAttribute("data-id");
    const node = graph.nodes.find((candidate) => candidate.key === key);
    if (!node || !pickable(node)) return;
    event.preventDefault();
    onSelect(node.key);
  };

  return (
    <div
      data-slot="flow-canvas"
      className={cn("h-full w-full", className)}
      onKeyDown={onKeyDown}
    >
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={NODE_TYPES}
        edgeTypes={EDGE_TYPES}
        onNodeClick={
          onSelect &&
          ((_event, node: FlowNodeType) => {
            if (pickable(node.data.node)) onSelect(node.id);
          })
        }
        onPaneClick={onSelect && (() => onSelect(null))}
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
