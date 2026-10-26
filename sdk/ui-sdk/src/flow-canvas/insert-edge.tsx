// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// A line of a flow on a canvas that inserts: drawn as React Flow's own line,
// with its label at its middle and, when the app inserts on it, a + after
// the label. The + is named for where it adds, by the line's label and its
// ends. A click on it gives the line to the app and goes no further, so it
// neither picks a node nor lets one go.
"use client";

import {
  BaseEdge,
  type Edge,
  EdgeLabelRenderer,
  type EdgeProps,
  getBezierPath,
} from "@xyflow/react";
import { PlusIcon } from "lucide-react";

import { Button } from "../button";

export type InsertEdgeData = {
  /** The line's label, or null when it has none. */
  label: string | null;
  sourceName: string;
  targetName: string;
  /** Inserts on this line; null leaves it without a +. */
  onInsert: (() => void) | null;
};

export type InsertEdgeType = Edge<InsertEdgeData, "insert">;

/** What the + adds on: the line's label, when it has one, and its ends. */
function insertName({ label, sourceName, targetName }: InsertEdgeData): string {
  const between = `between ${sourceName} and ${targetName}`;
  return label ? `Add on ${label}, ${between}` : `Add ${between}`;
}

export function InsertEdge({
  id,
  sourceX,
  sourceY,
  sourcePosition,
  targetX,
  targetY,
  targetPosition,
  style,
  data,
}: EdgeProps<InsertEdgeType>) {
  const [path, labelX, labelY] = getBezierPath({
    sourceX,
    sourceY,
    sourcePosition,
    targetX,
    targetY,
    targetPosition,
  });
  const onInsert = data?.onInsert ?? null;
  return (
    <>
      <BaseEdge id={id} path={path} style={style} />
      {data && (data.label || onInsert) && (
        <EdgeLabelRenderer>
          <div
            data-slot="flow-edge-label"
            className="nodrag nopan pointer-events-auto absolute flex items-center gap-1"
            style={{
              transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)`,
            }}
          >
            {data.label && (
              <span className="select-none rounded-[4px] bg-background px-1 text-[11.5px] text-muted-foreground">
                {data.label}
              </span>
            )}
            {onInsert && (
              <Button
                type="button"
                variant="outline"
                size="icon-xs"
                aria-label={insertName(data)}
                onClick={(event) => {
                  event.stopPropagation();
                  onInsert();
                }}
              >
                <PlusIcon aria-hidden />
              </Button>
            )}
          </div>
        </EdgeLabelRenderer>
      )}
    </>
  );
}
