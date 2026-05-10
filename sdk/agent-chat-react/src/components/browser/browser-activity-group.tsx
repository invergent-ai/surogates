import { useState } from "react";
import {
  AlertCircleIcon,
  ChevronDownIcon,
  ChevronRightIcon,
  ZapIcon,
} from "lucide-react";
import type { ToolCallInfo } from "../../types";

interface BrowserActivityGroupProps {
  calls: ToolCallInfo[];
}

export function BrowserActivityGroup({ calls }: BrowserActivityGroupProps) {
  const [open, setOpen] = useState(false);
  const latest = calls[calls.length - 1];

  return (
    <div className="rounded-none border border-line bg-card text-xs">
      <button
        type="button"
        onClick={() => setOpen((current) => !current)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left hover:bg-muted/40"
      >
        {open ? (
          <ChevronDownIcon className="size-3 shrink-0" aria-hidden="true" />
        ) : (
          <ChevronRightIcon className="size-3 shrink-0" aria-hidden="true" />
        )}
        <ZapIcon className="size-3 shrink-0 text-amber-500" aria-hidden="true" />
        <span className="font-semibold text-foreground">browser</span>
        <span className="min-w-0 truncate text-muted-foreground">
          ({calls.length} {calls.length === 1 ? "action" : "actions"} - latest:{" "}
          {latest ? summarise(latest) : "none"})
        </span>
      </button>
      {open && (
        <ul className="border-t border-line py-1">
          {calls.map((call) => {
            const error = stringValue(parseJson(call.result).error);
            return (
              <li
                key={call.id}
                className="flex items-center gap-2 px-6 py-1 font-mono text-[11px]"
              >
                {error && (
                  <AlertCircleIcon
                    data-testid={`activity-error-${call.id}`}
                    className="size-3 shrink-0 text-destructive"
                    aria-hidden="true"
                  />
                )}
                <span className="min-w-0 truncate">{summarise(call)}</span>
                {error && (
                  <span className="ml-auto max-w-40 truncate text-destructive">
                    {error}
                  </span>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

function summarise(call: ToolCallInfo): string {
  const verb = call.toolName.replace(/^browser_/, "");
  const args = parseJson(call.args);
  const ref = stringValue(args.ref);
  const url = stringValue(args.url);
  const text = stringValue(args.text);
  if (url) return `${verb} ${url}`;
  if (ref) return `${verb} ${ref}`;
  if (text) return `${verb} "${text.slice(0, 24)}"`;
  return verb;
}

function parseJson(raw?: string): Record<string, unknown> {
  if (!raw) return {};
  try {
    const parsed = JSON.parse(raw) as unknown;
    return typeof parsed === "object" && parsed !== null && !Array.isArray(parsed)
      ? parsed as Record<string, unknown>
      : {};
  } catch {
    return {};
  }
}

function stringValue(value: unknown): string {
  return typeof value === "string" ? value : "";
}
