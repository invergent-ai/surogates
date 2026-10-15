import {
  Eraser,
  Hand,
  Maximize2,
  MousePointer2,
  Pen,
  Redo2,
  Type,
  Undo2,
} from "lucide-react";
import type { ComponentType } from "react";
import { Button } from "@invergent/ui-sdk/button";
import { RabbitMark } from "@invergent/ui-sdk/rabbit-mark";
import { ToggleGroup, ToggleGroupItem } from "@invergent/ui-sdk/toggle-group";

export type WbTool = "pen" | "eraser" | "text" | "slot" | "select" | "pan";

export const INK_COLORS = [
  "#111827",
  "#2563eb",
  "#dc2626",
  "#16a34a",
  "#d97706",
] as const;

export const INK_WIDTHS = [2, 4, 8, 16] as const;

// `Icon` is any glyph taking a className -- the lucide set, plus our
// own brand mark, which is not a lucide icon and must not be typed as
// one.
const TOOLS: {
  id: WbTool;
  label: string;
  Icon: ComponentType<{ className?: string }>;
}[] = [
  { id: "pen", label: "Pen", Icon: Pen },
  { id: "eraser", label: "Eraser", Icon: Eraser },
  { id: "text", label: "Text", Icon: Type },
  // Where the agent should answer: click to place an answer box, or
  // drag one. The id stays "slot" -- that is what the object is.
  { id: "slot", label: "Answer box", Icon: RabbitMark },
  { id: "select", label: "Select", Icon: MousePointer2 },
  { id: "pan", label: "Pan", Icon: Hand },
];

export interface ToolRailProps {
  tool: WbTool;
  onToolChange(tool: WbTool): void;
  color: string;
  onColorChange(color: string): void;
  width: number;
  onWidthChange(width: number): void;
  canUndo: boolean;
  canRedo: boolean;
  onUndo(): void;
  onRedo(): void;
  /** Frame all content. The only reliable way home on an infinite
   *  canvas: pan far enough and no edge stops you. */
  onFit(): void;
  disabled?: boolean;
}

export function ToolRail({
  tool,
  onToolChange,
  color,
  onColorChange,
  width,
  onWidthChange,
  canUndo,
  canRedo,
  onUndo,
  onRedo,
  onFit,
  disabled,
}: ToolRailProps) {
  return (
    <div
      className="flex flex-col items-center gap-1 rounded-lg border bg-background p-1"
      role="toolbar"
      aria-label="Whiteboard tools"
      aria-orientation="vertical"
    >
      {/* One pick in each group: clicking the picked item again keeps it,
          rather than leaving the board with no tool, colour or width. The
          tools are modes, amber when picked; colour and width are options,
          picked with a neutral fill, since an amber one would hide the
          orange ink. */}
      <ToggleGroup
        type="single"
        orientation="vertical"
        aria-label="Tool"
        value={tool}
        onValueChange={(next) => {
          if (next) onToolChange(next as WbTool);
        }}
        disabled={disabled}
      >
        {TOOLS.map(({ id, label, Icon }) => (
          <ToggleGroupItem key={id} value={id} aria-label={label} size="icon">
            <Icon className="size-4" />
          </ToggleGroupItem>
        ))}
      </ToggleGroup>

      <div className="my-1 h-px w-full bg-border" />

      <ToggleGroup
        type="single"
        variant="outline"
        size="sm"
        orientation="vertical"
        aria-label="Colour"
        value={color}
        onValueChange={(next) => {
          if (next) onColorChange(next);
        }}
        disabled={disabled}
      >
        {INK_COLORS.map((c) => (
          <ToggleGroupItem key={c} value={c} aria-label={`Colour ${c}`} size="icon-xs">
            {/* The swatch is the colour itself; the item's fill marks the pick. */}
            <span className="size-5 rounded-full" style={{ backgroundColor: c }} />
          </ToggleGroupItem>
        ))}
      </ToggleGroup>

      <div className="my-1 h-px w-full bg-border" />

      <ToggleGroup
        type="single"
        variant="outline"
        size="sm"
        orientation="vertical"
        aria-label="Width"
        value={String(width)}
        onValueChange={(next) => {
          if (next) onWidthChange(Number(next));
        }}
        disabled={disabled}
      >
        {INK_WIDTHS.map((w) => (
          <ToggleGroupItem key={w} value={String(w)} aria-label={`Width ${w}`} size="icon-xs">
            <span
              className="rounded-full bg-foreground"
              style={{ width: w + 2, height: w + 2 }}
            />
          </ToggleGroupItem>
        ))}
      </ToggleGroup>

      <div className="my-1 h-px w-full bg-border" />

      <Button
        type="button"
        size="icon"
        variant="ghost"
        aria-label="Fit to content"
        disabled={disabled}
        onClick={onFit}
      >
        <Maximize2 className="size-4" />
      </Button>

      <Button
        type="button"
        size="icon"
        variant="ghost"
        aria-label="Undo"
        disabled={disabled || !canUndo}
        onClick={onUndo}
      >
        <Undo2 className="size-4" />
      </Button>
      <Button
        type="button"
        size="icon"
        variant="ghost"
        aria-label="Redo"
        disabled={disabled || !canRedo}
        onClick={onRedo}
      >
        <Redo2 className="size-4" />
      </Button>
    </div>
  );
}
