// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Inline artifact block rendered in the chat timeline.  Loads the
// artifact payload on-demand from the API server the first time it
// renders; the event carries only metadata.

import { lazy, Suspense, useEffect, useState } from "react";
import { CheckIcon, CopyIcon, DownloadIcon } from "lucide-react";
import {
  Artifact,
  ArtifactAction,
  ArtifactActions,
  ArtifactContent,
  ArtifactDescription,
  ArtifactHeader,
  ArtifactTitle,
} from "@/components/ai-elements/artifact";
import { Shimmer } from "@/components/ai-elements/shimmer";
import { getArtifact } from "@/api/artifacts";
import type { ArtifactKind, ArtifactPayload } from "@/types/session";
import { ArtifactMarkdown } from "./artifact-markdown";
import { ArtifactTable } from "./artifact-table";
import { ArtifactHtml } from "./artifact-html";
import { ArtifactSvg } from "./artifact-svg";
import {
  copyText,
  downloadText,
  exportArtifact,
  safeFilename,
} from "./artifact-export";

// Vega ships a big bundle — only pay for it when a chart actually
// appears in the thread.
const ArtifactChart = lazy(() =>
  import("./artifact-chart").then((m) => ({ default: m.ArtifactChart })),
);

interface ArtifactBlockProps {
  sessionId: string;
  artifactId: string;
  name: string;
  kind: ArtifactKind;
  version: number;
}

const KIND_LABEL: Record<ArtifactKind, string> = {
  markdown: "Markdown document",
  table: "Table",
  chart: "Chart",
  html: "HTML preview",
  svg: "SVG",
};

// How long the copy icon shows the green check before reverting.
const COPY_FEEDBACK_MS = 1500;

export function ArtifactBlock({
  sessionId,
  artifactId,
  name,
  kind,
  version,
}: ArtifactBlockProps) {
  const [payload, setPayload] = useState<ArtifactPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setPayload(null);
    setError(null);
    getArtifact(sessionId, artifactId)
      .then((p) => {
        if (!cancelled) setPayload(p);
      })
      .catch((e: unknown) => {
        if (!cancelled) {
          setError(e instanceof Error ? e.message : "Failed to load artifact");
        }
      });
    return () => {
      cancelled = true;
    };
  }, [sessionId, artifactId, version]);

  const handleCopy = async () => {
    if (!payload) return;
    const { text } = exportArtifact(payload);
    const ok = await copyText(text);
    if (ok) {
      setCopied(true);
      setTimeout(() => setCopied(false), COPY_FEEDBACK_MS);
    }
  };

  const handleDownload = () => {
    if (!payload) return;
    const { text, mime, extension } = exportArtifact(payload);
    downloadText(`${safeFilename(name)}.${extension}`, text, mime);
  };

  return (
    <Artifact className="my-2 w-full border-border">
      <ArtifactHeader>
        <div className="flex min-w-0 flex-col">
          <ArtifactTitle className="truncate">{name}</ArtifactTitle>
          <ArtifactDescription>
            {error ?? (payload ? KIND_LABEL[kind] : "Loading…")}
          </ArtifactDescription>
        </div>
        <ArtifactActions>
          <ArtifactAction
            tooltip={copied ? "Copied!" : "Copy"}
            label="Copy artifact"
            icon={copied ? CheckIcon : CopyIcon}
            disabled={!payload}
            onClick={handleCopy}
            className={copied ? "text-emerald-500 hover:text-emerald-500" : ""}
          />
          <ArtifactAction
            tooltip="Download"
            label="Download artifact"
            icon={DownloadIcon}
            disabled={!payload}
            onClick={handleDownload}
          />
        </ArtifactActions>
      </ArtifactHeader>
      <ArtifactContent>
        <ArtifactBody error={error} payload={payload} />
      </ArtifactContent>
    </Artifact>
  );
}

function ArtifactBody({
  error,
  payload,
}: {
  error: string | null;
  payload: ArtifactPayload | null;
}) {
  if (error) {
    return <p className="text-sm text-destructive">{error}</p>;
  }
  if (!payload) {
    return (
      <Shimmer duration={5} className="text-sm text-muted-foreground">
        Loading artifact…
      </Shimmer>
    );
  }
  switch (payload.kind) {
    case "markdown":
      return <ArtifactMarkdown spec={payload.spec} />;
    case "table":
      return <ArtifactTable spec={payload.spec} />;
    case "chart":
      return (
        <Suspense
          fallback={
            <Shimmer duration={5} className="text-sm text-muted-foreground">
              Loading chart…
            </Shimmer>
          }
        >
          <ArtifactChart spec={payload.spec} />
        </Suspense>
      );
    case "html":
      return <ArtifactHtml spec={payload.spec} />;
    case "svg":
      return <ArtifactSvg spec={payload.spec} />;
  }
}
