// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// One node of a flow on the canvas: who does it, or its kind's icon, and its
// name, over the kind in a word. A traced node is lit amber when walked or
// open, and faded when not reached yet. Its handles only anchor the edges:
// nothing connects to them.
"use client";

import { Handle, type Node, type NodeProps, Position } from "@xyflow/react";
import {
  CircleIcon,
  CirclePlayIcon,
  GitBranchIcon,
  HourglassIcon,
  LogOutIcon,
  type LucideIcon,
  MergeIcon,
  ScaleIcon,
  SplitIcon,
  SquareIcon,
  VoteIcon,
} from "lucide-react";

import { cn } from "../lib/utils";
import { PerformerMark } from "../performer-mark";
import {
  FLOW_NODE_HEIGHT,
  FLOW_NODE_WIDTH,
  type FlowGraphNode,
  type FlowTraceState,
} from "./graph";

const KINDS = new Map<string, { word: string; icon: LucideIcon }>([
  ["start", { word: "Start", icon: CirclePlayIcon }],
  ["step", { word: "Step", icon: SquareIcon }],
  ["vote", { word: "Vote", icon: VoteIcon }],
  ["decision", { word: "Decision", icon: GitBranchIcon }],
  ["rule", { word: "Rule", icon: ScaleIcon }],
  ["split", { word: "Split", icon: SplitIcon }],
  ["join", { word: "Join", icon: MergeIcon }],
  ["wait", { word: "Wait", icon: HourglassIcon }],
  ["exit", { word: "Exit", icon: LogOutIcon }],
]);

/** A kind in a word and an icon; a kind the canvas does not know reads as itself. */
function kindOf(kind: string): { word: string; icon: LucideIcon } {
  return (
    KINDS.get(kind) ?? {
      word: kind.charAt(0).toUpperCase() + kind.slice(1),
      icon: CircleIcon,
    }
  );
}

// The flow node travels whole instead of being spread into `data`, so none
// of its fields can collide with React Flow's own.
export type FlowNodeData = {
  node: FlowGraphNode;
  /** Its trace state, or null when nothing is traced. */
  state: FlowTraceState | null;
};

export type FlowNodeType = Node<FlowNodeData, "flow">;

export function FlowNodeView({ data }: NodeProps<FlowNodeType>) {
  const { node, state } = data;
  const { word, icon: Icon } = kindOf(node.kind);
  return (
    <div
      data-slot="flow-node"
      data-state={state ?? undefined}
      style={{ width: FLOW_NODE_WIDTH, height: FLOW_NODE_HEIGHT }}
      className={cn(
        "rounded-[10px] border border-border bg-background px-3 py-2 shadow-sm",
        state === "done" && "border-amber-500/50 bg-amber-500/5",
        state === "current" &&
          "border-amber-500 bg-amber-500/10 shadow-md shadow-amber-500/30",
        state === "ahead" && "opacity-40",
      )}
    >
      {node.kind !== "start" && (
        <Handle type="target" position={Position.Top} isConnectable={false} />
      )}
      <div className="flex min-w-0 items-center gap-2">
        {node.performer ? (
          <PerformerMark kind={node.performer} className="shrink-0" />
        ) : (
          <Icon
            aria-hidden
            className="size-3.5 shrink-0 text-muted-foreground"
          />
        )}
        <span title={node.name} className="truncate font-medium text-[13px]">
          {node.name}
        </span>
      </div>
      <p className="mt-0.5 text-[11.5px] text-muted-foreground">{word}</p>
      {node.kind !== "exit" && (
        <Handle
          type="source"
          position={Position.Bottom}
          isConnectable={false}
        />
      )}
    </div>
  );
}
