// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Compact renderer for skill_manage CRUD calls.

import { parseArgs } from "./shared";
import type { ToolCallInfo } from "../../../types";

interface SkillManageArgs {
  action?: string;
  name?: string;
  file_path?: string;
}

interface SkillManageResult {
  success?: boolean;
  error?: string;
  message?: string;
  path?: string;
}

const ACTION_LABEL: Record<string, string> = {
  create: "Create skill",
  patch: "Patch skill",
  edit: "Edit skill",
  delete: "Delete skill",
  write_file: "Write skill file",
  remove_file: "Remove skill file",
};

export function SkillManageToolBlock({ tc }: { tc: ToolCallInfo }) {
  const args = parseArgs<SkillManageArgs>(tc.args) ?? {};
  const result = tc.result ? parseArgs<SkillManageResult>(tc.result) : null;
  const action = args.action ?? "manage";
  const label = ACTION_LABEL[action] ?? "Manage skill";
  const target = args.file_path ? `${args.name ?? "?"}/${args.file_path}` : (args.name ?? "?");
  const failed = result?.success === false || Boolean(result?.error);
  const summary = result?.error ?? result?.message ?? result?.path;

  return (
    <div className="flex items-center gap-1.5 text-sm">
      <span className="font-semibold text-foreground">{label}</span>
      <span className="text-muted-foreground truncate">{target}</span>
      {summary && (
        <span className={failed ? "text-red-500 truncate" : "text-muted-foreground/70 truncate"}>
          → {summary}
        </span>
      )}
    </div>
  );
}
