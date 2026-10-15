// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// TurnDownloads — the files a turn produced, listed under its last answer
// the way Claude lists them: one card per file. No recap, no box around them.

import type { AgentChatTurnArtifactRef } from "../../types";
import { WorkspaceFileCard } from "./workspace-file-card";

export interface TurnDownloadsProps {
  sessionId: string;
  /** The turn's ``kind: "file"`` refs, in the order they are listed. */
  files: AgentChatTurnArtifactRef[];
}

export function TurnDownloads({ sessionId, files }: TurnDownloadsProps) {
  return (
    <div data-testid="turn-downloads" className="mt-3 space-y-2">
      {files.map((file, i) => (
        <WorkspaceFileCard
          key={`${file.ref}:${i}`}
          sessionId={sessionId}
          path={file.ref}
          label={file.label}
        />
      ))}
    </div>
  );
}
