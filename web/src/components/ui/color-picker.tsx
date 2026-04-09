// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import { cn } from "@/utils/cn";
import { Check } from "lucide-react";

const COLORS = [
  "#EF4444",
  "#F97316",
  "#F59E0B",
  "#EAB308",
  "#84CC16",
  "#22C55E",
  "#10B981",
  "#14B8A6",
  "#06B6D4",
  "#0EA5E9",
  "#3B82F6",
  "#6366F1",
  "#8B5CF6",
  "#A855F7",
  "#D946EF",
  "#EC4899",
];

interface ColorPickerProps {
  value?: string;
  onValueChange?: (color: string) => void;
  colors?: string[];
  className?: string;
}

function ColorPicker({
  value,
  onValueChange,
  colors = COLORS,
  className,
}: ColorPickerProps) {
  return (
    <div
      data-slot="color-picker"
      className={cn("grid grid-cols-8 gap-1.5", className)}
    >
      {colors.map((color) => (
        <button
          key={color}
          type="button"
          onClick={() => onValueChange?.(color)}
          className={cn(
            "size-6 rounded-md cursor-pointer transition-all duration-100 flex items-center justify-center",
            value === color
              ? "ring-2 ring-offset-2 ring-offset-background"
              : "hover:scale-110",
          )}
          style={{
            backgroundColor: color,
            ...(value === color ? { ringColor: color } : {}),
          }}
        >
          {value === color && <Check className="size-3.5 text-white drop-shadow-sm" />}
        </button>
      ))}
    </div>
  );
}

export { ColorPicker, COLORS };
