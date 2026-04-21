// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Sub-agent library page (mirrors the skills page).  Lists agents
// grouped by source (platform / org / user), lets the user view any
// agent's full definition, and lets them create / edit / delete
// user-scoped agents.  Platform and org agents are read-only from
// this UI.

import { useCallback, useEffect, useMemo, useState } from "react";
import { dump as yamlDump } from "js-yaml";
import {
  BotIcon,
  FileTextIcon,
  Loader2Icon,
  PencilIcon,
  PlusIcon,
  SearchIcon,
  SaveIcon,
  TrashIcon,
  UsersIcon,
} from "lucide-react";
import { toast } from "sonner";

import {
  type AgentDetail,
  type AgentSource,
  type AgentSummary,
  createAgent,
  deleteAgent,
  getAgent,
  listAgents,
  updateAgent,
} from "@/api/agents";
import { useAppStore } from "@/stores/app-store";
import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Field,
  FieldGroup,
  FieldLabel,
} from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import {
  InputGroup,
  InputGroupAddon,
  InputGroupInput,
} from "@/components/ui/input-group";
import { Textarea } from "@/components/ui/textarea";
import { SessionSidebar } from "@/components/navbar";
import { MessageResponse } from "@/components/ai-elements/message";

const SOURCE_LABELS: Record<AgentSource, string> = {
  platform: "Built-in",
  org: "Organization",
  user: "My sub-agents",
};

const SOURCE_ORDER: AgentSource[] = ["user", "org", "platform"];

const AGENT_TEMPLATE =
  "---\n" +
  "name: my-agent\n" +
  "description: One-line description Claude will see when deciding to delegate\n" +
  "tools: [read_file, search_files, terminal]\n" +
  "disallowed_tools: [write_file, patch]\n" +
  "model: claude-sonnet-4-6\n" +
  "max_iterations: 20\n" +
  "---\n\n" +
  "You are a specialized sub-agent.  Describe your persona, focus\n" +
  "areas, and output format here.\n";

export function AgentsPage() {
  const fetchUser = useAppStore((s) => s.fetchUser);
  const fetchSessions = useAppStore((s) => s.fetchSessions);

  const [agents, setAgents] = useState<AgentSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState("");

  const [selectedName, setSelectedName] = useState<string | null>(null);
  const [detail, setDetail] = useState<AgentDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  const [createOpen, setCreateOpen] = useState(false);
  const [editOpen, setEditOpen] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<AgentDetail | null>(null);

  useEffect(() => {
    void fetchSessions();
    void fetchUser();
  }, [fetchSessions, fetchUser]);

  const loadAgents = useCallback(async () => {
    setLoading(true);
    try {
      const response = await listAgents();
      setAgents(response.agents);
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadAgents();
  }, [loadAgents]);

  useEffect(() => {
    if (!selectedName) {
      setDetail(null);
      return;
    }
    setDetailLoading(true);
    let cancelled = false;
    getAgent(selectedName)
      .then((result) => {
        if (!cancelled) setDetail(result);
      })
      .catch((err: Error) => {
        if (!cancelled) {
          toast.error(err.message);
          setDetail(null);
        }
      })
      .finally(() => {
        if (!cancelled) setDetailLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedName]);

  const filtered = useMemo(() => {
    const query = search.trim().toLowerCase();
    return agents.filter((agent) => {
      if (!query) return true;
      const haystack = [
        agent.name,
        agent.description,
        agent.category ?? "",
        agent.model ?? "",
      ]
        .join(" ")
        .toLowerCase();
      return haystack.includes(query);
    });
  }, [agents, search]);

  const grouped = useMemo(() => {
    const buckets: Record<AgentSource, AgentSummary[]> = {
      user: [],
      org: [],
      platform: [],
    };
    for (const agent of filtered) buckets[agent.source].push(agent);
    return buckets;
  }, [filtered]);

  const isUserAgent = detail?.source === "user";

  const handleCreated = useCallback(
    async (name: string) => {
      setCreateOpen(false);
      await loadAgents();
      setSelectedName(name);
    },
    [loadAgents],
  );

  const handleEdited = useCallback(
    async (name: string) => {
      setEditOpen(false);
      toast.success(`Sub-agent '${name}' updated.`);
      // Refetch both sides: the edit dialog sends raw AGENT.md text,
      // which the server re-parses -- any field changed in frontmatter
      // (description, model, tools, ...) must be re-read so the
      // sidebar summary and the detail view's MetaRow / ConfigPanel
      // stay in sync with what got saved.
      await loadAgents();
      try {
        const fresh = await getAgent(name);
        setDetail(fresh);
      } catch (err) {
        toast.error((err as Error).message);
      }
    },
    [loadAgents],
  );

  const handleDelete = useCallback(async () => {
    if (!deleteTarget) return;
    try {
      await deleteAgent(deleteTarget.name);
      toast.success(`Sub-agent '${deleteTarget.name}' deleted.`);
      setAgents((prev) => prev.filter((a) => a.name !== deleteTarget.name));
      if (selectedName === deleteTarget.name) setSelectedName(null);
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      setDeleteTarget(null);
    }
  }, [deleteTarget, selectedName]);

  return (
    <>
      <SessionSidebar />
      <main className="flex-1 flex overflow-hidden">
        <section className="flex flex-col w-80 min-w-80 border-r border-line">
          <header className="px-4 py-4 border-b border-line space-y-3">
            <div className="flex items-center justify-between gap-2">
              <h1 className="text-lg font-bold tracking-tight text-foreground">
                Sub-agents
              </h1>
              <Button
                size="sm"
                onClick={() => setCreateOpen(true)}
                className="gap-1.5"
              >
                <PlusIcon className="w-3.5 h-3.5" />
                New
              </Button>
            </div>

            <InputGroup>
              <InputGroupAddon>
                <SearchIcon />
              </InputGroupAddon>
              <InputGroupInput
                placeholder="Search sub-agents..."
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
            </InputGroup>
          </header>

          <div className="flex-1 overflow-y-auto">
            {loading ? (
              <div className="flex items-center justify-center py-12 text-muted-foreground">
                <Loader2Icon className="w-4 h-4 animate-spin mr-2" />
                Loading...
              </div>
            ) : filtered.length === 0 ? (
              <div className="text-center py-12 px-4 space-y-2">
                <BotIcon className="w-8 h-8 text-muted-foreground/40 mx-auto" />
                <p className="text-sm text-muted-foreground">
                  {search
                    ? "No sub-agents match your search."
                    : "No sub-agents configured yet."}
                </p>
              </div>
            ) : (
              SOURCE_ORDER.map((source) => {
                const items = grouped[source];
                if (items.length === 0) return null;
                return (
                  <AgentGroup
                    key={source}
                    label={SOURCE_LABELS[source]}
                    items={items}
                    selectedName={selectedName}
                    onSelect={setSelectedName}
                  />
                );
              })
            )}
          </div>
        </section>

        <section className="flex-1 overflow-y-auto">
          {detailLoading ? (
            <div className="flex items-center justify-center h-full text-muted-foreground">
              <Loader2Icon className="w-4 h-4 animate-spin mr-2" />
              Loading sub-agent...
            </div>
          ) : !detail ? (
            <EmptyState />
          ) : (
            <AgentDetailView
              detail={detail}
              isUserAgent={isUserAgent}
              onEdit={() => setEditOpen(true)}
              onDelete={() => setDeleteTarget(detail)}
            />
          )}
        </section>
      </main>

      <CreateAgentDialog
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        onCreated={handleCreated}
      />

      {detail && isUserAgent && (
        <EditAgentDialog
          open={editOpen}
          agent={detail}
          onClose={() => setEditOpen(false)}
          onEdited={handleEdited}
        />
      )}

      <ConfirmDialog
        open={deleteTarget !== null}
        title="Delete sub-agent?"
        description={
          deleteTarget
            ? `This permanently deletes '${deleteTarget.name}'.  Coordinators referencing this agent_type will fall back to an error on spawn.`
            : ""
        }
        confirmLabel="Delete"
        variant="destructive"
        onConfirm={handleDelete}
        onCancel={() => setDeleteTarget(null)}
      />
    </>
  );
}

function AgentGroup({
  label,
  items,
  selectedName,
  onSelect,
}: {
  label: string;
  items: AgentSummary[];
  selectedName: string | null;
  onSelect: (name: string) => void;
}) {
  return (
    <div className="py-1">
      <div className="px-4 pt-3 pb-1 text-xs font-semibold uppercase tracking-wide text-faint">
        {label}
      </div>
      {items.map((agent) => {
        const isActive = agent.name === selectedName;
        return (
          <button
            type="button"
            key={`${agent.source}-${agent.name}`}
            onClick={() => onSelect(agent.name)}
            className={cn(
              "w-full text-left px-4 py-2 border-l-2 transition-colors",
              isActive
                ? "bg-line text-foreground border-l-primary"
                : "border-l-transparent hover:bg-input text-subtle hover:text-foreground",
            )}
          >
            <div className="flex items-center gap-1.5 min-w-0">
              <div className="font-medium text-sm truncate flex-1">
                {agent.name}
              </div>
              {!agent.enabled && (
                <Badge variant="outline" className="text-[10px]">
                  disabled
                </Badge>
              )}
            </div>
            {agent.description && (
              <div className="text-xs text-faint truncate mt-0.5">
                {agent.description}
              </div>
            )}
          </button>
        );
      })}
    </div>
  );
}

function EmptyState() {
  return (
    <div className="flex flex-col items-center justify-center h-full text-center px-8 gap-3">
      <UsersIcon className="w-10 h-10 text-muted-foreground/40" />
      <div className="space-y-1">
        <p className="text-sm text-muted-foreground">Select a sub-agent</p>
        <p className="text-sm text-faint max-w-sm">
          Sub-agents are preset bundles of system prompt, tool filter,
          model, and iteration cap.  A coordinator session spawns
          children with ``agent_type=&lt;name&gt;`` to apply the bundle.
        </p>
      </div>
    </div>
  );
}

function AgentDetailView({
  detail,
  isUserAgent,
  onEdit,
  onDelete,
}: {
  detail: AgentDetail;
  isUserAgent: boolean;
  onEdit: () => void;
  onDelete: () => void;
}) {
  return (
    <article className="max-w-3xl mx-auto px-8 py-8">
      <header className="mb-6 pb-6 border-b border-line">
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2 mb-2">
              <BotIcon className="w-5 h-5 text-muted-foreground shrink-0" />
              <h2 className="text-2xl font-bold tracking-tight truncate">
                {detail.name}
              </h2>
              {!detail.enabled && (
                <Badge variant="outline">disabled</Badge>
              )}
            </div>
            {detail.description && (
              <p className="text-sm text-muted-foreground">
                {detail.description}
              </p>
            )}
          </div>

          {isUserAgent && (
            <div className="flex items-center gap-2 shrink-0">
              <Button
                variant="outline"
                size="sm"
                onClick={onEdit}
                className="gap-1.5"
              >
                <PencilIcon className="w-3.5 h-3.5" />
                Edit
              </Button>
              <Button
                variant="ghost"
                size="sm"
                onClick={onDelete}
                className="text-muted-foreground hover:text-destructive"
              >
                <TrashIcon className="w-4 h-4" />
              </Button>
            </div>
          )}
        </div>

        <MetaRow detail={detail} />
      </header>

      <ConfigPanel detail={detail} />

      <section>
        <h3 className="text-sm font-semibold uppercase tracking-wide text-faint mb-2">
          System prompt
        </h3>
        <div className="prose prose-sm dark:prose-invert max-w-none">
          <MessageResponse>{detail.system_prompt}</MessageResponse>
        </div>
      </section>
    </article>
  );
}

function MetaRow({ detail }: { detail: AgentDetail }) {
  const items: { label: string; value: string }[] = [
    { label: "Source", value: SOURCE_LABELS[detail.source] },
  ];
  if (detail.category) items.push({ label: "Category", value: detail.category });
  if (detail.tags && detail.tags.length > 0) {
    items.push({ label: "Tags", value: detail.tags.join(", ") });
  }

  return (
    <dl className="mt-4 flex flex-wrap gap-x-6 gap-y-1 text-sm">
      {items.map((item) => (
        <div key={item.label} className="flex items-center gap-1.5">
          <dt className="text-faint">{item.label}:</dt>
          <dd className="text-subtle">{item.value}</dd>
        </div>
      ))}
    </dl>
  );
}

function ConfigPanel({ detail }: { detail: AgentDetail }) {
  const hasConfig =
    detail.model ||
    detail.max_iterations ||
    detail.policy_profile ||
    (detail.tools && detail.tools.length > 0) ||
    (detail.disallowed_tools && detail.disallowed_tools.length > 0);
  if (!hasConfig) return null;

  return (
    <section className="mb-6 rounded-lg border border-line bg-card px-4 py-3 space-y-2">
      <div className="flex items-center gap-2">
        <FileTextIcon className="w-4 h-4 text-muted-foreground" />
        <h3 className="text-sm font-semibold text-foreground">
          Configuration
        </h3>
      </div>

      <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-sm">
        {detail.model && (
          <>
            <dt className="text-faint">Model</dt>
            <dd className="text-subtle font-mono text-xs truncate">
              {detail.model}
            </dd>
          </>
        )}
        {detail.max_iterations != null && (
          <>
            <dt className="text-faint">Max iterations</dt>
            <dd className="text-subtle">{detail.max_iterations}</dd>
          </>
        )}
        {detail.policy_profile && (
          <>
            <dt className="text-faint">Policy profile</dt>
            <dd className="text-subtle font-mono text-xs">
              {detail.policy_profile}
            </dd>
          </>
        )}
        {detail.tools && detail.tools.length > 0 && (
          <>
            <dt className="text-faint">Allowed tools</dt>
            <dd className="text-subtle">{detail.tools.join(", ")}</dd>
          </>
        )}
        {detail.disallowed_tools && detail.disallowed_tools.length > 0 && (
          <>
            <dt className="text-faint">Disallowed tools</dt>
            <dd className="text-subtle">
              {detail.disallowed_tools.join(", ")}
            </dd>
          </>
        )}
      </dl>
    </section>
  );
}

function CreateAgentDialog({
  open,
  onClose,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  onCreated: (name: string) => void | Promise<void>;
}) {
  const [name, setName] = useState("");
  const [category, setCategory] = useState("");
  const [content, setContent] = useState(AGENT_TEMPLATE);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!open) {
      setName("");
      setCategory("");
      setContent(AGENT_TEMPLATE);
    }
  }, [open]);

  const handleSubmit = useCallback(async () => {
    if (!name.trim() || !content.trim()) return;
    setSaving(true);
    try {
      await createAgent({
        name: name.trim(),
        content,
        category: category.trim() || null,
      });
      toast.success(`Sub-agent '${name}' created.`);
      await onCreated(name.trim());
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      setSaving(false);
    }
  }, [name, category, content, onCreated]);

  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>New sub-agent</DialogTitle>
          <DialogDescription>
            Create a sub-agent visible only to you.  The ``name`` field
            in the frontmatter must match the name entered here.
          </DialogDescription>
        </DialogHeader>

        <form
          onSubmit={(e) => {
            e.preventDefault();
            void handleSubmit();
          }}
        >
          <FieldGroup>
            <Field orientation="vertical">
              <FieldLabel htmlFor="agent-name">Name</FieldLabel>
              <Input
                id="agent-name"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="code-reviewer"
                required
              />
            </Field>

            <Field orientation="vertical">
              <FieldLabel htmlFor="agent-category">Category (optional)</FieldLabel>
              <Input
                id="agent-category"
                value={category}
                onChange={(e) => setCategory(e.target.value)}
                placeholder="e.g. review, research"
              />
            </Field>

            <Field orientation="vertical">
              <FieldLabel htmlFor="agent-content">AGENT.md content</FieldLabel>
              <Textarea
                id="agent-content"
                value={content}
                onChange={(e) => setContent(e.target.value)}
                rows={18}
                className="font-mono text-xs"
                required
              />
            </Field>
          </FieldGroup>

          <DialogFooter className="mt-6">
            <Button type="button" variant="outline" onClick={onClose}>
              Cancel
            </Button>
            <Button
              type="submit"
              disabled={saving || !name.trim() || !content.trim()}
              className="gap-2"
            >
              {saving ? (
                <Loader2Icon className="w-4 h-4 animate-spin" />
              ) : (
                <PlusIcon className="w-4 h-4" />
              )}
              Create
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function EditAgentDialog({
  open,
  agent,
  onClose,
  onEdited,
}: {
  open: boolean;
  agent: AgentDetail;
  onClose: () => void;
  onEdited: (name: string) => void;
}) {
  // Lazy init -- the reconstruction runs YAML serialisation, which we
  // want to skip when the dialog is closed.
  const [content, setContent] = useState(() =>
    open ? buildEditorContent(agent) : "",
  );
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (open) setContent(buildEditorContent(agent));
  }, [open, agent]);

  const handleSubmit = useCallback(async () => {
    if (!content.trim()) return;
    setSaving(true);
    try {
      await updateAgent(agent.name, content);
      onEdited(agent.name);
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      setSaving(false);
    }
  }, [content, agent.name, onEdited]);

  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>Edit '{agent.name}'</DialogTitle>
          <DialogDescription>
            Replace the full AGENT.md content.  The ``name`` in the
            frontmatter must stay ``{agent.name}``.
          </DialogDescription>
        </DialogHeader>

        <form
          onSubmit={(e) => {
            e.preventDefault();
            void handleSubmit();
          }}
        >
          <FieldGroup>
            <Field orientation="vertical">
              <FieldLabel htmlFor="agent-edit-content">
                AGENT.md content
              </FieldLabel>
              <Textarea
                id="agent-edit-content"
                value={content}
                onChange={(e) => setContent(e.target.value)}
                rows={20}
                className="font-mono text-xs"
                required
              />
            </Field>
          </FieldGroup>

          <DialogFooter className="mt-6">
            <Button type="button" variant="outline" onClick={onClose}>
              Cancel
            </Button>
            <Button
              type="submit"
              disabled={saving || !content.trim()}
              className="gap-2"
            >
              {saving ? (
                <Loader2Icon className="w-4 h-4 animate-spin" />
              ) : (
                <SaveIcon className="w-4 h-4" />
              )}
              Save
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function buildEditorContent(agent: AgentDetail): string {
  // The server returns fields individually, so the edit form rebuilds a
  // frontmatter block the user can tweak.  Delegating to js-yaml keeps
  // us out of the quoting-edge-case business (colons, reserved words,
  // number-looking strings, control characters).
  const fm: Record<string, unknown> = { name: agent.name };
  if (agent.description) fm.description = agent.description;
  if (agent.tools && agent.tools.length > 0) fm.tools = agent.tools;
  if (agent.disallowed_tools && agent.disallowed_tools.length > 0) {
    fm.disallowed_tools = agent.disallowed_tools;
  }
  if (agent.model) fm.model = agent.model;
  if (agent.max_iterations != null) fm.max_iterations = agent.max_iterations;
  if (agent.policy_profile) fm.policy_profile = agent.policy_profile;
  if (agent.category) fm.category = agent.category;
  if (agent.tags && agent.tags.length > 0) fm.tags = agent.tags;
  if (!agent.enabled) fm.enabled = false;

  // ``flowLevel: 1`` keeps top-level mapping block-style (each key on
  // its own line) while lists render inline ([a, b, c]) to match the
  // AGENT_TEMPLATE shown in the create dialog.
  const frontmatter = yamlDump(fm, {
    flowLevel: 1,
    quotingType: '"',
    forceQuotes: false,
  }).trimEnd();
  return `---\n${frontmatter}\n---\n\n${agent.system_prompt}\n`;
}
