// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import { memo } from "react";
import {
  File as FileIcon,
  FileArchive,
  FileAudio,
  FileCode,
  FileImage,
  FileSpreadsheet,
  FileText,
  FileVideo,
} from "lucide-react";
import {
  Message,
  MessageContent,
  MessageResponse,
} from "../ai-elements/message";
import {
  Reasoning,
  ReasoningContent,
  ReasoningTrigger,
} from "../ai-elements/reasoning";
import { ToolCallBlock } from "./tool-call-block";
import { useSmoothStream } from "./use-smooth-stream";
import type {
  AgentChatDisplayAttachment,
  ChatMessage as ChatMessageType,
} from "../../types";

interface ChatMessageProps {
  message: ChatMessageType;
  isLast: boolean;
  onFileSelect?: (path: string) => void;
}

export const ChatMessage = memo(function ChatMessage({
  message,
  isLast,
  onFileSelect,
}: ChatMessageProps) {
  if (message.role === "user") {
    return (
      <Message from="user">
        <MessageContent>
          {message.content}
          {message.images && message.images.length > 0 && (
            <div className="flex flex-wrap gap-2 mt-2">
              {message.images.map((img, i) => (
                <img
                  key={i}
                  src={img.data}
                  alt="Attached"
                  className="max-w-64 max-h-48 rounded-lg border border-border object-contain"
                />
              ))}
            </div>
          )}
          {message.attachments && message.attachments.length > 0 && (
            <div className="flex flex-wrap gap-2 mt-2">
              {message.attachments.map((a, i) => (
                <AttachmentChip
                  // Stable key across the optimistic → confirmed
                  // transition: the local chip starts without a path
                  // and gains one when the user.message SSE event
                  // arrives; falling back to the array index keeps the
                  // chip in place across that swap.
                  key={a.path ?? `pending-${i}`}
                  attachment={a}
                  onSelect={onFileSelect}
                />
              ))}
            </div>
          )}
        </MessageContent>
      </Message>
    );
  }

  return <AssistantMessage message={message} isLast={isLast} onFileSelect={onFileSelect} />;
});

// ── Attachment chip ──────────────────────────────────────────────────

function iconForMime(mime?: string) {
  if (!mime) return FileIcon;
  if (mime.startsWith("image/")) return FileImage;
  if (mime.startsWith("audio/")) return FileAudio;
  if (mime.startsWith("video/")) return FileVideo;
  if (mime === "application/pdf") return FileText;
  if (mime.startsWith("text/")) return FileText;
  if (
    mime === "application/json" ||
    mime === "application/xml" ||
    mime.endsWith("+xml") ||
    mime.endsWith("+json") ||
    mime.includes("javascript") ||
    mime.includes("typescript")
  ) {
    return FileCode;
  }
  if (
    mime === "application/zip" ||
    mime === "application/x-7z-compressed" ||
    mime === "application/x-tar" ||
    mime === "application/gzip" ||
    mime === "application/x-rar-compressed"
  ) {
    return FileArchive;
  }
  if (
    mime === "text/csv" ||
    mime === "application/vnd.ms-excel" ||
    mime.includes("spreadsheet")
  ) {
    return FileSpreadsheet;
  }
  return FileIcon;
}

function formatBytes(n?: number): string {
  if (n == null || !Number.isFinite(n) || n < 0) return "";
  for (const [unit, divisor] of [
    ["GB", 1_000_000_000],
    ["MB", 1_000_000],
    ["KB", 1_000],
  ] as const) {
    if (n >= divisor) return `${(n / divisor).toFixed(1)} ${unit}`;
  }
  return `${n} B`;
}

function AttachmentChip({
  attachment,
  onSelect,
}: {
  attachment: AgentChatDisplayAttachment;
  onSelect?: (path: string) => void;
}) {
  const Icon = iconForMime(attachment.mimeType);
  const clickable = !!attachment.path && !!onSelect;
  const sizeLabel = formatBytes(attachment.size);
  return (
    <button
      type="button"
      onClick={
        clickable ? () => onSelect!(attachment.path!) : undefined
      }
      disabled={!clickable}
      title={attachment.filename}
      aria-label={
        clickable
          ? `Open ${attachment.filename}`
          : `${attachment.filename} (uploading)`
      }
      className={
        "inline-flex items-center gap-2 rounded-md border border-border " +
        "bg-muted/40 px-2 py-1 text-xs max-w-[16rem] " +
        (clickable
          ? "hover:bg-muted cursor-pointer"
          : "opacity-60 cursor-default")
      }
    >
      <Icon className="size-3.5 shrink-0" />
      <span className="truncate">{attachment.filename}</span>
      {sizeLabel && (
        <span className="text-muted-foreground shrink-0">{sizeLabel}</span>
      )}
    </button>
  );
}

function AssistantMessage({
  message,
  isLast,
  onFileSelect,
}: {
  message: ChatMessageType;
  isLast: boolean;
  onFileSelect?: (path: string) => void;
}) {
  const hasReasoning = !!message.reasoning;
  const hasToolCalls = !!(message.toolCalls && message.toolCalls.length > 0);
  const isStreaming = message.status === "streaming" && isLast;
  // Guard: if content is identical to reasoning, skip it (dedup).
  const hasContent = !!(
    message.content &&
    message.content !== message.reasoning
  );
  const reasoningIsStreaming = isStreaming && !hasContent && !hasToolCalls;
  const displayedContent = useSmoothStream(message.content ?? "", isStreaming);
  const displayedReasoning = useSmoothStream(
    message.reasoning ?? "",
    reasoningIsStreaming,
  );

  return (
    <Message from="assistant">
      <MessageContent>
        {hasReasoning && (
          <Reasoning isStreaming={reasoningIsStreaming}>
            <ReasoningTrigger />
            <ReasoningContent>{displayedReasoning}</ReasoningContent>
          </Reasoning>
        )}

        {hasToolCalls && (
          <div className="w-full flex flex-col gap-8">
            {message.toolCalls!.map((tc) => (
              <ToolCallBlock key={tc.id} tc={tc} onFileSelect={onFileSelect} />
            ))}
          </div>
        )}

        {hasContent && (
          <MessageResponse>{displayedContent}</MessageResponse>
        )}

        {isStreaming && !hasContent && !hasToolCalls && !hasReasoning && (
          <div className="flex items-center gap-1.5 py-1 text-muted-foreground">
            <span className="inline-block size-1.5 animate-pulse rounded-full bg-primary" />
            <span className="text-xs">Thinking...</span>
          </div>
        )}
      </MessageContent>
    </Message>
  );
}
