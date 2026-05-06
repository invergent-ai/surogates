import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "../ui/collapsible";
import { cn } from "../../lib/utils";
import {
  ChevronRightIcon,
  FileIcon,
  FolderIcon,
  FolderOpenIcon,
} from "lucide-react";
import type { HTMLAttributes, ReactNode } from "react";
import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
} from "react";

interface FileTreeContextType {
  expandedPaths: Set<string>;
  togglePath: (path: string) => void;
  selectedPath?: string;
  onSelect?: (path: string) => void;
}

const noop = () => {};

const FileTreeContext = createContext<FileTreeContextType>({
  expandedPaths: new Set(),
  togglePath: noop,
});

export type FileTreeProps = Omit<HTMLAttributes<HTMLDivElement>, "onSelect"> & {
  expanded?: Set<string>;
  defaultExpanded?: Set<string>;
  selectedPath?: string | null;
  onSelect?: (path: string) => void;
  onExpandedChange?: (expanded: Set<string>) => void;
};

export function FileTree({
  expanded: controlledExpanded,
  defaultExpanded = new Set(),
  selectedPath,
  onSelect,
  onExpandedChange,
  className,
  children,
  ...props
}: FileTreeProps) {
  const [internalExpanded, setInternalExpanded] = useState(defaultExpanded);
  const expandedPaths = controlledExpanded ?? internalExpanded;

  const togglePath = useCallback(
    (path: string) => {
      const next = new Set(expandedPaths);
      if (next.has(path)) {
        next.delete(path);
      } else {
        next.add(path);
      }
      setInternalExpanded(next);
      onExpandedChange?.(next);
    },
    [expandedPaths, onExpandedChange],
  );

  const contextValue = useMemo(
    () => ({
      expandedPaths,
      onSelect,
      selectedPath: selectedPath ?? undefined,
      togglePath,
    }),
    [expandedPaths, onSelect, selectedPath, togglePath],
  );

  return (
    <FileTreeContext.Provider value={contextValue}>
      <div
        className={cn("rounded-lg border bg-background text-sm", className)}
        role="tree"
        {...props}
      >
        <div className="p-2">{children}</div>
      </div>
    </FileTreeContext.Provider>
  );
}

export type FileTreeIconProps = HTMLAttributes<HTMLSpanElement>;

export function FileTreeIcon({
  className,
  children,
  ...props
}: FileTreeIconProps) {
  return (
    <span className={cn("shrink-0", className)} {...props}>
      {children}
    </span>
  );
}

export type FileTreeNameProps = HTMLAttributes<HTMLSpanElement>;

export function FileTreeName({
  className,
  children,
  ...props
}: FileTreeNameProps) {
  return (
    <span className={cn("truncate", className)} {...props}>
      {children}
    </span>
  );
}

export type FileTreeFolderProps = HTMLAttributes<HTMLDivElement> & {
  path: string;
  name: string;
};

export function FileTreeFolder({
  path,
  name,
  className,
  children,
  ...props
}: FileTreeFolderProps) {
  const { expandedPaths, togglePath, selectedPath, onSelect } =
    useContext(FileTreeContext);
  const isExpanded = expandedPaths.has(path);
  const isSelected = selectedPath === path;

  const handleOpenChange = useCallback(() => {
    togglePath(path);
  }, [togglePath, path]);

  const handleSelect = useCallback(() => {
    onSelect?.(path);
  }, [onSelect, path]);

  return (
    <Collapsible onOpenChange={handleOpenChange} open={isExpanded}>
      <div className={cn("", className)} role="treeitem" tabIndex={0} {...props}>
        <div
          className={cn(
            "flex w-full items-center gap-1 rounded px-2 py-1 text-left transition-colors hover:bg-muted/50",
            isSelected && "bg-muted",
          )}
        >
          <CollapsibleTrigger asChild>
            <button
              className="flex shrink-0 cursor-pointer items-center border-none bg-transparent p-0"
              type="button"
              aria-label={isExpanded ? `Collapse ${name}` : `Expand ${name}`}
            >
              <ChevronRightIcon
                className={cn(
                  "size-4 shrink-0 text-muted-foreground transition-transform",
                  isExpanded && "rotate-90",
                )}
              />
            </button>
          </CollapsibleTrigger>
          <button
            className="flex min-w-0 flex-1 cursor-pointer items-center gap-1 border-none bg-transparent p-0 text-left"
            onClick={handleSelect}
            type="button"
          >
            <FileTreeIcon>
              {isExpanded ? (
                <FolderOpenIcon className="size-4 text-blue-500" />
              ) : (
                <FolderIcon className="size-4 text-blue-500" />
              )}
            </FileTreeIcon>
            <FileTreeName>{name}</FileTreeName>
          </button>
        </div>
        <CollapsibleContent>
          <div className="ml-4 border-l border-muted-foreground/50 pl-2">
            {children}
          </div>
        </CollapsibleContent>
      </div>
    </Collapsible>
  );
}

export type FileTreeFileProps = HTMLAttributes<HTMLDivElement> & {
  path: string;
  name: string;
  icon?: ReactNode;
};

export function FileTreeFile({
  path,
  name,
  icon,
  className,
  children,
  ...props
}: FileTreeFileProps) {
  const { selectedPath, onSelect } = useContext(FileTreeContext);
  const isSelected = selectedPath === path;

  const handleClick = useCallback(() => {
    onSelect?.(path);
  }, [onSelect, path]);

  const handleKeyDown = useCallback(
    (event: React.KeyboardEvent) => {
      if (event.key === "Enter" || event.key === " ") {
        onSelect?.(path);
      }
    },
    [onSelect, path],
  );

  return (
    <div
      className={cn(
        "group flex cursor-pointer items-center gap-1 rounded px-2 py-1 transition-colors hover:bg-muted/50",
        isSelected && "bg-muted",
        className,
      )}
      onClick={handleClick}
      onKeyDown={handleKeyDown}
      role="treeitem"
      tabIndex={0}
      {...props}
    >
      {children ?? (
        <>
          <span className="size-4 shrink-0" />
          <FileTreeIcon>
            {icon ?? <FileIcon className="size-4 text-muted-foreground" />}
          </FileTreeIcon>
          <FileTreeName>{name}</FileTreeName>
        </>
      )}
    </div>
  );
}

