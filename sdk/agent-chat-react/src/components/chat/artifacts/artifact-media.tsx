// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Renderers for file-backed media artifacts (generated images/videos).
// The spec only carries a workspace-relative path — the bytes stream
// through the authenticated workspace download URL, so large media
// never sits in the artifact store, the event log, or component state.

import { useMemo } from "react";
import { useAgentChatAdapterContext } from "../../../adapter-context";
import type { AgentChatMediaArtifactSpec } from "../../../types";
import { cn } from "../../../lib/utils";

interface ArtifactMediaProps {
  sessionId: string;
  spec: AgentChatMediaArtifactSpec;
  fill?: boolean;
}

function useMediaSrc(sessionId: string, path: string): string {
  const { adapter } = useAgentChatAdapterContext();
  return useMemo(
    () => adapter.getWorkspaceDownloadUrl({ sessionId, path }),
    [adapter, sessionId, path],
  );
}

function MediaCaption({ caption }: { caption?: string | null }) {
  if (!caption) return null;
  return (
    <figcaption className="border-t border-border px-3 py-2 text-xs leading-snug text-muted-foreground">
      {caption}
    </figcaption>
  );
}

export function ArtifactImage({ sessionId, spec, fill = false }: ArtifactMediaProps) {
  const src = useMediaSrc(sessionId, spec.path);
  return (
    <figure className="m-0">
      <img
        src={src}
        alt={spec.caption ?? spec.path}
        loading="lazy"
        className={cn(
          "block w-full bg-muted/30 object-contain",
          fill ? "max-h-full" : "max-h-[480px]",
        )}
      />
      <MediaCaption caption={spec.caption} />
    </figure>
  );
}

export function ArtifactVideo({ sessionId, spec, fill = false }: ArtifactMediaProps) {
  const src = useMediaSrc(sessionId, spec.path);
  return (
    <figure className="m-0">
      {/* biome-ignore lint/a11y/useMediaCaption: generated videos have no caption track */}
      <video
        src={src}
        controls
        preload="metadata"
        className={cn(
          "block w-full bg-black object-contain",
          fill ? "max-h-full" : "max-h-[480px]",
        )}
      />
      <MediaCaption caption={spec.caption} />
    </figure>
  );
}
