// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Sub-agent library page (mirrors the skills page).  Lists agents
// grouped by source (platform / org / user), lets the user view any
// agent's full definition, and lets them create / edit / delete
// user-scoped agents.  Platform and org agents are read-only from
// this UI.

import { type ReactNode, useCallback, useEffect, useMemo, useState } from "react";
import { dump as yamlDump } from "js-yaml";
import {
  BotIcon,
  ChevronLeftIcon,
  FileTextIcon,
  Loader2Icon,
  PencilIcon,
  PlusIcon,
  SearchIcon,
  SaveIcon,
  TrashIcon,
} from "lucide-react";
import { MessageResponse } from "@invergent/agent-chat-react";
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
import { Button } from "@invergent/ui-sdk/button";
import { TagPill, TonePill } from "@invergent/ui-sdk/chip";
import { ConfirmDialog } from "@invergent/ui-sdk/confirm-dialog";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@invergent/ui-sdk/dialog";
import {
  Field,
  FieldGroup,
  FieldLabel,
} from "@invergent/ui-sdk/field";
import { EmptyState } from "@invergent/ui-sdk/empty-state";
import { Input } from "@invergent/ui-sdk/input";
import {
  InputGroup,
  InputGroupAddon,
  InputGroupInput,
} from "@invergent/ui-sdk/input-group";
import { ListRow } from "@invergent/ui-sdk/list-row";
import { SectionLabel } from "@invergent/ui-sdk/section-label";
import { TextLink } from "@invergent/ui-sdk/text-link";
import { Textarea } from "@invergent/ui-sdk/textarea";
import { AppShell } from "@/components/app-shell";
import { SessionSidebar } from "@/components/navbar";

const SOURCE_LABELS: Record<AgentSource, string> = {
  platform: "Built-in",
  org: "Organization",
  user: "My sub-agents",
};

const SOURCE_ORDER: AgentSource[] = ["user", "org", "platform"];

// The list's first group label starts right under the header, whose rule it would double.
const FIRST_GROUP = "first:mt-0 first:border-t-0 first:pt-1";

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
    <AppShell sidebar={<SessionSidebar />}>
      <div className="flex-1 flex flex-col md:flex-row overflow-hidden">
        <section
          className={cn(
            "flex flex-col border-r border-line",
            "w-full md:w-72 md:min-w-72 lg:w-80 lg:min-w-80",
            selectedName && "hidden md:flex",
          )}
        >
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
              <EmptyState
                className="px-4 py-12"
                title={
                  search
                    ? "No sub-agents match your search"
                    : "No sub-agents configured yet"
                }
              />
            ) : (
              <div className="flex flex-col gap-0.5 p-2">
                {SOURCE_ORDER.map((source) => {
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
                })}
              </div>
            )}
          </div>
        </section>

        <section
          className={cn(
            "flex-1 flex-col overflow-y-auto",
            selectedName ? "flex" : "hidden md:flex",
          )}
        >
          {selectedName && (
            <div className="md:hidden flex items-center border-b border-line px-4 py-1">
              <TextLink asChild variant="muted">
                <button
                  type="button"
                  onClick={() => setSelectedName(null)}
                  aria-label="Back to sub-agents list"
                >
                  <ChevronLeftIcon className="size-4" />
                  Sub-agents
                </button>
              </TextLink>
            </div>
          )}
          {detailLoading ? (
            <div className="flex items-center justify-center h-full text-muted-foreground">
              <Loader2Icon className="w-4 h-4 animate-spin mr-2" />
              Loading sub-agent...
            </div>
          ) : !detail ? (
            <EmptyState
              className="h-full justify-center px-8"
              title="Select a sub-agent"
              description="Sub-agents are preset bundles of system prompt, tool filter, model, and iteration cap.  A coordinator session spawns children with ``agent_type=<name>`` to apply the bundle."
            />
          ) : (
            <AgentDetailView
              detail={detail}
              isUserAgent={isUserAgent}
              onEdit={() => setEditOpen(true)}
              onDelete={() => setDeleteTarget(detail)}
            />
          )}
        </section>
      </div>

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
    </AppShell>
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
    <>
      <SectionLabel label={label} className={FIRST_GROUP} />
      {items.map((agent) => (
        <ListRow
          key={`${agent.source}-${agent.name}`}
          icon={BotIcon}
          label={agent.name}
          description={agent.description || undefined}
          badge={!agent.enabled && <TonePill>Disabled</TonePill>}
          isActive={agent.name === selectedName}
          onClick={() => onSelect(agent.name)}
        />
      ))}
    </>
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
    <article className="w-full px-8 py-8">
      <header className="mb-6 pb-6 border-b border-line">
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2 mb-2">
              <BotIcon className="w-5 h-5 text-muted-foreground shrink-0" />
              <h2 className="text-2xl font-bold tracking-tight truncate">
                {detail.name}
              </h2>
              {!detail.enabled && <TonePill>Disabled</TonePill>}
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
  const items: { label: string; value: ReactNode }[] = [
    { label: "Source", value: SOURCE_LABELS[detail.source] },
  ];
  if (detail.category) items.push({ label: "Category", value: detail.category });
  if (detail.tags && detail.tags.length > 0) {
    items.push({
      label: "Tags",
      value: (
        <span className="flex flex-wrap gap-1">
          {detail.tags.map((tag) => (
            <TagPill key={tag} label={tag} />
          ))}
        </span>
      ),
    });
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
            <dd className="text-subtle  text-xs truncate">
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
            <dd className="text-subtle  text-xs">
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
                className=" text-xs"
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
                className=" text-xs"
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
