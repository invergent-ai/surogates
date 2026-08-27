import {
  Eraser,
  Hand,
  Maximize2,
  MousePointer2,
  Pen,
  Redo2,
  Undo2,
} from "lucide-react";
import { cn } from "../../lib/utils";
import { Button } from "../ui/button";

export type WbTool = "pen" | "eraser" | "select" | "pan";

export const INK_COLORS = [
  "#111827",
  "#2563eb",
  "#dc2626",
  "#16a34a",
  "#d97706",
] as const;

export const INK_WIDTHS = [2, 4, 8, 16] as const;

const TOOLS: { id: WbTool; label: string; Icon: typeof Pen }[] = [
  { id: "pen", label: "Pen", Icon: Pen },
  { id: "eraser", label: "Eraser", Icon: Eraser },
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
      className="flex flex-col gap-1 rounded-lg border bg-background p-1"
      role="toolbar"
      aria-label="Whiteboard tools"
              aria-orientation="vertical"
    >
      {TOOLS.map(({ id, label, Icon }) => (
        <Button
          key={id}
          type="button"
          size="icon"
          variant={tool === id ? "default" : "ghost"}
          aria-label={label}
          aria-pressed={tool === id}
          disabled={disabled}
          onClick={() => onToolChange(id)}
        >
          <Icon className="size-4" />
        </Button>
      ))}

      <div className="my-1 h-px bg-border" />

      {INK_COLORS.map((c) => (
        <button
          key={c}
          type="button"
          aria-label={`Colour ${c}`}
          aria-pressed={color === c}
          disabled={disabled}
          onClick={() => onColorChange(c)}
          className={cn(
            "mx-auto size-5 rounded-full border-2",
            color === c ? "border-foreground" : "border-transparent",
          )}
          style={{ backgroundColor: c }}
        />
      ))}

      <div className="my-1 h-px bg-border" />

      {INK_WIDTHS.map((w) => (
        <button
          key={w}
          type="button"
          aria-label={`Width ${w}`}
          aria-pressed={width === w}
          disabled={disabled}
          onClick={() => onWidthChange(w)}
          className={cn(
            "mx-auto flex size-6 items-center justify-center rounded",
            width === w ? "bg-accent" : "",
          )}
        >
          <span
            className="rounded-full bg-foreground"
            style={{ width: w + 2, height: w + 2 }}
          />
        </button>
      ))}

      <div className="my-1 h-px bg-border" />

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
