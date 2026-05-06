"use client";

import { cn } from "../../lib/utils";
import type { CSSProperties, ElementType } from "react";
import { memo, useMemo } from "react";

export interface TextShimmerProps {
  children: string;
  as?: ElementType;
  className?: string;
  duration?: number;
  spread?: number;
}

const ShimmerComponent = ({
  children,
  as: Component = "p",
  className,
  duration = 2,
  spread = 2,
}: TextShimmerProps) => {
  const dynamicSpread = useMemo(
    () => (children?.length ?? 0) * spread,
    [children, spread]
  );

  return (
    <Component
      className={cn("shimmer text-muted-foreground", className)}
      style={
        {
          "--shimmer-duration": duration * 1000,
          "--shimmer-spread": `${dynamicSpread}px`,
        } as CSSProperties
      }
    >
      {children}
    </Component>
  );
};

export const Shimmer = memo(ShimmerComponent);
