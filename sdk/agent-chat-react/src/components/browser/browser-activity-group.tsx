import { ZapIcon } from "lucide-react";
import type { ToolCallInfo } from "../../types";

interface BrowserActivityGroupProps {
  calls: ToolCallInfo[];
}

export function BrowserActivityGroup({ calls }: BrowserActivityGroupProps) {
  return (
    <div className="flex items-center gap-2 border border-line bg-card px-3 py-2 text-xs">
      <ZapIcon className="size-3 shrink-0 text-amber-500" aria-hidden="true" />
      <span className="shrink-0 font-semibold text-foreground">Browser:</span>
      <span className="min-w-0 truncate text-muted-foreground">
        {calls.map((call, index) => {
          const error = stringValue(parseJson(call.result).error);
          return (
            <span key={call.id}>
              {index > 0 && ", "}
              <span
                className={error ? "text-destructive" : undefined}
                data-testid={error ? `activity-error-${call.id}` : undefined}
                title={error || undefined}
              >
                {summarize(call)}
              </span>
            </span>
          );
        })}
      </span>
    </div>
  );
}

function summarize(call: ToolCallInfo): string {
  const args = parseJson(call.args);
  const handler = ACTION_SUMMARIES[call.toolName];
  return handler ? handler(args) : call.toolName.replace(/^browser_/, "");
}

const ACTION_SUMMARIES: Record<string, (args: Record<string, unknown>) => string> = {
  browser_navigate: (args) => `navigate to ${stringValue(args.url) || "?"}`,
  browser_get_state: () => "get state",
  browser_evaluate: () => "run script",
  browser_close: () => "close",
  browser_click: (args) => {
    const ref = stringValue(args.ref);
    if (ref) return `click ${ref}`;
    const x = asNumber(args.x);
    const y = asNumber(args.y);
    if (x !== null && y !== null) return `click (${x}, ${y})`;
    return "click";
  },
  // Never the text: what the agent typed is often a password, and a host
  // may feed events it read from the database, which the server never masked.
  browser_type: (args) => {
    const ref = stringValue(args.ref);
    const chars = [...stringValue(args.text)].length;
    const what = stringValue(args.secret_ref)
      ? "a stored secret"
      : chars
        ? `${chars} ${chars === 1 ? "char" : "chars"}`
        : "";
    const typed = what ? `type ${what}` : "type";
    return ref ? `${typed} into ${ref}` : typed;
  },
  browser_press_key: (args) => {
    const keys = Array.isArray(args.keys) ? args.keys.map(String) : [];
    return keys.length ? `press ${keys.join("+")}` : "press key";
  },
  browser_scroll: (args) => {
    const x = asNumber(args.x);
    const y = asNumber(args.y);
    const dx = asNumber(args.delta_x) ?? 0;
    const dy = asNumber(args.delta_y) ?? 0;
    const delta = dx || dy ? `Δ(${dx}, ${dy})` : "";
    const origin = x !== null && y !== null ? `at (${x}, ${y})` : "";
    return ["scroll", delta, origin].filter(Boolean).join(" ");
  },
  browser_drag: (args) => {
    const path = Array.isArray(args.path) ? args.path : [];
    if (path.length < 2) return "drag";
    const first = path[0] as unknown[];
    const last = path[path.length - 1] as unknown[];
    const point = (p: unknown[]) =>
      `(${asNumber(p[0]) ?? "?"}, ${asNumber(p[1]) ?? "?"})`;
    return `drag ${point(first)} → ${point(last)}`;
  },
  browser_wait: (args) => {
    const ms = asNumber(args.ms);
    return ms === null ? "wait" : `wait ${ms}ms`;
  },
  browser_screenshot: (args) =>
    args.annotate === true ? "screenshot (annotated)" : "screenshot",
};

function asNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
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
