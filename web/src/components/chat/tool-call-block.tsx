// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Main tool call block — dispatches to the appropriate renderer
// based on tool name.

import type { ToolCallInfo } from "@/hooks/use-session-runtime";

import { TerminalToolBlock } from "./tools/terminal-tool";
import { TodoToolBlock } from "./tools/todo-tool";
import { ExecuteCodeToolBlock } from "./tools/execute-code-tool";
import { SessionSearchBlock, WebToolBlock } from "./tools/oneliner-tools";
import { ReadFileBlock, WriteFileBlock, PatchBlock, SearchFilesBlock, ListFilesBlock } from "./tools/file-tools";
import { ProcessToolBlock } from "./tools/process-tool";
import { ExpertToolBlock } from "./tools/expert-tool";
import { SkillsListBlock, SkillViewBlock } from "./tools/skill-tools";
import { ClarifyToolBlock } from "./tools/clarify-tool";
import { ArtifactToolBlock } from "./tools/artifact-tool";
import { DelegateToolBlock } from "./tools/delegate-tool";
import { DefaultToolBlock } from "./tools/default-tool";

export function ToolCallBlock({ tc, onFileSelect }: { tc: ToolCallInfo; onFileSelect?: (path: string) => void }) {
  switch (tc.toolName) {
    case "terminal":
      return <TerminalToolBlock tc={tc} />;

    case "todo":
      return <TodoToolBlock tc={tc} />;

    case "execute_code":
      return <ExecuteCodeToolBlock tc={tc} />;

    case "session_search":
      return <SessionSearchBlock tc={tc} />;

    case "web_extract":
    case "web_search":
    case "web_crawl":
      return <WebToolBlock tc={tc} />;

    case "read_file":
      return <ReadFileBlock tc={tc} onFileSelect={onFileSelect} />;

    case "write_file":
      return <WriteFileBlock tc={tc} onFileSelect={onFileSelect} />;

    case "patch":
      return <PatchBlock tc={tc} onFileSelect={onFileSelect} />;

    case "search_files":
      return <SearchFilesBlock tc={tc} />;

    case "list_files":
      return <ListFilesBlock tc={tc} />;

    case "process":
      return <ProcessToolBlock tc={tc} />;

    case "consult_expert":
      return <ExpertToolBlock tc={tc} />;

    case "skills_list":
      return <SkillsListBlock tc={tc} />;

    case "skill_view":
      return <SkillViewBlock tc={tc} />;

    case "clarify":
      return <ClarifyToolBlock tc={tc} />;

    case "create_artifact":
      return <ArtifactToolBlock tc={tc} />;

    case "delegate_task":
      return <DelegateToolBlock tc={tc} />;

    default:
      return <DefaultToolBlock tc={tc} />;
  }
}
