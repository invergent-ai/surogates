"use client";

import {
  Collapsible,
  CollapsibleContent,
} from "@invergent/ui-sdk/collapsible";
import { CollapsibleHeader } from "@invergent/ui-sdk/collapsible-header";
import {
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "@invergent/ui-sdk/tabs";
import { cn } from "../../lib/utils";
import type { ToolUIPart } from "ai";
import { Code } from "lucide-react";
import type { ComponentProps } from "react";

import { getStatusBadge } from "./tool";

export type SandboxRootProps = ComponentProps<typeof Collapsible>;

export const Sandbox = ({ className, ...props }: SandboxRootProps) => (
  <Collapsible
    className={cn(
      "not-prose mb-4 w-full overflow-hidden rounded-[10px] border border-foreground/[0.08]",
      className
    )}
    defaultOpen
    {...props}
  />
);

export interface SandboxHeaderProps {
  title?: string;
  state: ToolUIPart["state"];
  className?: string;
}

export const SandboxHeader = ({
  className,
  title,
  state,
  ...props
}: SandboxHeaderProps) => (
  <CollapsibleHeader
    variant="section"
    icon={Code}
    className={className}
    {...props}
  >
    <span className="flex items-center gap-2">
      {title}
      {getStatusBadge(state)}
    </span>
  </CollapsibleHeader>
);

export type SandboxContentProps = ComponentProps<typeof CollapsibleContent>;

export const SandboxContent = ({
  className,
  ...props
}: SandboxContentProps) => (
  <CollapsibleContent
    className={cn(
      "data-[state=closed]:fade-out-0 data-[state=closed]:slide-out-to-top-2 data-[state=open]:slide-in-from-top-2 outline-none data-[state=closed]:animate-out data-[state=open]:animate-in",
      className
    )}
    {...props}
  />
);

export type SandboxTabsProps = ComponentProps<typeof Tabs>;

export const SandboxTabs = ({ className, ...props }: SandboxTabsProps) => (
  <Tabs className={cn("w-full gap-0", className)} {...props} />
);

export type SandboxTabsBarProps = ComponentProps<"div">;

export const SandboxTabsBar = ({
  className,
  ...props
}: SandboxTabsBarProps) => (
  <div
    className={cn(
      "flex w-full items-center border-foreground/[0.08] border-t border-b px-2 py-1.5",
      className
    )}
    {...props}
  />
);

export type SandboxTabsListProps = ComponentProps<typeof TabsList>;

export const SandboxTabsList = ({
  className,
  ...props
}: SandboxTabsListProps) => (
  <TabsList variant="line" className={className} {...props} />
);

export type SandboxTabsTriggerProps = ComponentProps<typeof TabsTrigger>;

export const SandboxTabsTrigger = ({
  className,
  ...props
}: SandboxTabsTriggerProps) => (
  <TabsTrigger className={className} {...props} />
);

export type SandboxTabContentProps = ComponentProps<typeof TabsContent>;

export const SandboxTabContent = ({
  className,
  ...props
}: SandboxTabContentProps) => (
  <TabsContent className={cn("mt-0 text-sm", className)} {...props} />
);
