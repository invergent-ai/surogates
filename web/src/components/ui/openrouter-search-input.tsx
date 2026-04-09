import { useState, useEffect, useRef } from "react";
import { cn } from "@/utils/cn";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { ChevronsUpDown, Loader2, Search } from "lucide-react";
import { OpenRouter } from "@openrouter/sdk";
import type { Model } from "@openrouter/sdk/models/model.js";

interface OpenRouterSearchInputProps {
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  className?: string;
  disabled?: boolean;
}

let cachedModels: Model[] | null = null;

async function fetchOpenRouterModels(): Promise<Model[]> {
  if (cachedModels) return cachedModels;
  const client = new OpenRouter();
  const res = await client.models.list();
  cachedModels = res.data;
  return cachedModels;
}

export function OpenRouterSearchInput({
  value,
  onChange,
  placeholder = "openai/gpt-4o",
  className,
  disabled,
}: OpenRouterSearchInputProps) {
  const [allModels, setAllModels] = useState<Model[]>(cachedModels ?? []);
  const [loading, setLoading] = useState(false);
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const containerRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);

  // Fetch models on first open
  useEffect(() => {
    if (!open || allModels.length > 0) return;
    setLoading(true);
    fetchOpenRouterModels()
      .then(setAllModels)
      .catch(() => {})
      .finally(() => setLoading(false));
  }, [open, allModels.length]);

  // Close on outside click
  useEffect(() => {
    if (!open) return;
    const handler = (e: MouseEvent) => {
      if (
        containerRef.current &&
        !containerRef.current.contains(e.target as Node)
      ) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [open]);

  // Focus search when opened
  useEffect(() => {
    if (open) searchRef.current?.focus();
  }, [open]);

  const lowerQuery = query.toLowerCase();
  const filtered = query.length < 2
    ? allModels.slice(0, 50)
    : allModels.filter(
        (m) =>
          m.id.toLowerCase().includes(lowerQuery) ||
          m.name.toLowerCase().includes(lowerQuery),
      ).slice(0, 50);

  const handleSelect = (modelId: string) => {
    onChange(modelId);
    setOpen(false);
    setQuery("");
  };

  const formatPrice = (price: string | undefined | null) => {
    if (!price) return null;
    const n = parseFloat(price);
    if (n === 0) return "free";
    // price is per token, show per 1M tokens
    return `$${(n * 1_000_000).toFixed(2)}/M`;
  };

  return (
    <div ref={containerRef} className={cn("relative min-w-0 w-full", className)}>
      {/* Trigger */}
      <Tooltip>
        <TooltipTrigger asChild>
          <button
            type="button"
            disabled={disabled}
            onClick={() => setOpen((v) => !v)}
            className={cn(
              "flex w-full items-center justify-between h-8 rounded-lg border border-input bg-transparent px-2.5 text-xs transition-colors cursor-pointer",
              "hover:border-ring/50 focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 outline-none",
              value ? "text-foreground font-mono" : "text-muted-foreground",
            )}
          >
            <span className="flex-1 w-0 truncate text-left text-muted-foreground">
              {value || placeholder}
            </span>
            <ChevronsUpDown
              size={12}
              className="shrink-0 ml-2 text-muted-foreground/50"
            />
          </button>
        </TooltipTrigger>
        {value && (
          <TooltipContent side="top" className="max-w-sm break-all font-mono">
            {value}
          </TooltipContent>
        )}
      </Tooltip>

      {/* Dropdown panel */}
      {open && (
        <div className="absolute z-50 mt-1 w-full rounded-lg border border-border bg-popover shadow-md overflow-hidden">
          {/* Header */}
          <div className="flex items-center gap-1.5 border-b border-border bg-muted/30 px-2 h-7">
            <span className="text-[10px] text-muted-foreground/60 tracking-wide font-display">
              Search OpenRouter Models…
            </span>
          </div>

          {/* Search */}
          <div className="relative border-b border-border">
            <Search
              size={12}
              className="absolute left-2.5 top-1/2 -translate-y-1/2 text-muted-foreground/50"
            />
            <input
              ref={searchRef}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search models…"
              className="w-full h-7 pl-7 pr-2 text-xs bg-transparent border-none outline-none placeholder:text-muted-foreground/40"
            />
            {loading && (
              <Loader2
                size={12}
                className="absolute right-2.5 top-1/2 -translate-y-1/2 animate-spin text-muted-foreground"
              />
            )}
          </div>

          {/* Results */}
          <div className="h-52 overflow-y-auto">
            {filtered.length === 0 ? (
              <div className="flex items-center justify-center h-full text-xs text-muted-foreground/50">
                {loading ? "Loading models…" : "No results"}
              </div>
            ) : (
              filtered.map((m) => {
                const inputPrice = formatPrice(m.pricing.prompt);
                const outputPrice = formatPrice(m.pricing.completion);
                return (
                  <Tooltip key={m.id}>
                    <TooltipTrigger asChild>
                      <button
                        type="button"
                        onClick={() => handleSelect(m.id)}
                        className={cn(
                          "flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs transition-colors cursor-pointer border-none bg-transparent",
                          "hover:bg-accent/60",
                          value === m.id && "bg-accent/40",
                        )}
                      >
                        <div className="flex-1 min-w-0">
                          <span className="font-mono text-foreground truncate block">
                            {m.id}
                          </span>
                          <div className="flex items-center gap-2 text-[10px] text-muted-foreground mt-0.5">
                            {m.contextLength && (
                              <span>{(m.contextLength / 1024).toFixed(0)}k ctx</span>
                            )}
                            {inputPrice && <span>{inputPrice} in</span>}
                            {outputPrice && <span>{outputPrice} out</span>}
                          </div>
                        </div>
                      </button>
                    </TooltipTrigger>
                    <TooltipContent
                      side="right"
                      className="max-w-xs text-xs"
                    >
                      <div className="font-semibold">{m.name}</div>
                      {m.description && (
                        <div className="text-muted-foreground mt-0.5 line-clamp-3">
                          {m.description}
                        </div>
                      )}
                    </TooltipContent>
                  </Tooltip>
                );
              })
            )}
          </div>
        </div>
      )}
    </div>
  );
}
