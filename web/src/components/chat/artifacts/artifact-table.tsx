// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Table artifact renderer — a compact HTML table with a horizontal
// scroll area for wide datasets.  No sorting/filtering yet; just a
// faithful display of the spec.

import type { TableArtifactSpec } from "@/types/session";

function formatCell(value: unknown): string {
  if (value == null) return "";
  if (typeof value === "number" || typeof value === "string") return String(value);
  if (typeof value === "boolean") return value ? "true" : "false";
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

export function ArtifactTable({ spec }: { spec: TableArtifactSpec }) {
  const columns = spec.columns ?? [];
  const rows = spec.rows ?? [];

  return (
    <div className="overflow-auto">
      <table className="w-full border-collapse text-sm">
        {spec.caption && (
          <caption className="pb-2 text-left text-xs text-muted-foreground">
            {spec.caption}
          </caption>
        )}
        <thead>
          <tr className="border-b border-border">
            {columns.map((col) => (
              <th
                key={col}
                className="px-3 py-2 text-left font-medium text-foreground"
              >
                {col}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr
              key={i}
              className="border-b border-border/50 last:border-b-0 hover:bg-muted/50"
            >
              {columns.map((col) => (
                <td
                  key={col}
                  className="px-3 py-2 align-top text-muted-foreground"
                >
                  {formatCell(row[col])}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
