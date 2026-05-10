import { cn } from "../../lib/utils";
import type { AgentChatBrowserState } from "../../types";

export function BrowserStatusDot({
  status,
}: {
  status: AgentChatBrowserState["status"];
}) {
  return (
    <span
      aria-hidden="true"
      className={cn(
        "inline-block size-2 shrink-0 rounded-full",
        status === "live" && "bg-emerald-500",
        status === "user-control" && "bg-amber-500",
        status === "provisioning" && "animate-pulse bg-sky-500",
        status === "closed" && "bg-muted-foreground",
      )}
    />
  );
}
