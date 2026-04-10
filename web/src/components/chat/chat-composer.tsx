// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import { useRef, useState, type KeyboardEvent } from "react";
import { ArrowUpIcon, SquareIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

interface ChatComposerProps {
  onSend: (text: string) => void;
  onStop: () => void;
  isRunning: boolean;
  disabled?: boolean;
  placeholder?: string;
}

export function ChatComposer({
  onSend,
  onStop,
  isRunning,
  disabled = false,
  placeholder = "Send a message...",
}: ChatComposerProps) {
  const [value, setValue] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const canSend = value.trim().length > 0 && !isRunning && !disabled;

  const handleSend = () => {
    const text = value.trim();
    if (!text || isRunning || disabled) return;
    onSend(text);
    setValue("");
    // Reset textarea height.
    if (textareaRef.current) {
      textareaRef.current.style.height = "auto";
    }
  };

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const handleInput = () => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 200) + "px";
  };

  return (
    <div
      className={cn(
        "flex items-end gap-2 rounded-2xl border bg-background px-3 py-2",
        "transition-shadow focus-within:border-ring/75 focus-within:ring-2 focus-within:ring-ring/20",
      )}
    >
      <textarea
        ref={textareaRef}
        value={value}
        onChange={(e) => {
          setValue(e.target.value);
          handleInput();
        }}
        onKeyDown={handleKeyDown}
        placeholder={placeholder}
        disabled={disabled}
        rows={1}
        className={cn(
          "flex-1 resize-none bg-transparent text-sm outline-none",
          "placeholder:text-muted-foreground/60",
          "min-h-[2.25rem] max-h-[200px] py-1.5",
          disabled && "cursor-not-allowed opacity-50",
        )}
        autoFocus
      />

      {isRunning ? (
        <Button
          type="button"
          variant="default"
          size="icon"
          className="size-8 shrink-0 rounded-full"
          onClick={onStop}
          aria-label="Stop"
        >
          <SquareIcon className="size-3 fill-current" />
        </Button>
      ) : (
        <Button
          type="button"
          variant="default"
          size="icon"
          className="size-8 shrink-0 rounded-full"
          onClick={handleSend}
          disabled={!canSend}
          aria-label="Send"
        >
          <ArrowUpIcon className="size-4" />
        </Button>
      )}
    </div>
  );
}
