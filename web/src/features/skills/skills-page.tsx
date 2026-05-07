// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  BookOpenIcon,
  FileTextIcon,
  Loader2Icon,
  PencilIcon,
  PlusIcon,
  SaveIcon,
  SearchIcon,
  SparklesIcon,
  TrashIcon,
} from "lucide-react";
import { MessageResponse } from "@invergent/agent-chat-react";
import { toast } from "sonner";
import {
  createSkill,
  deleteSkill,
  getSkill,
  listSkills,
  type SkillDetail,
  type SkillKind,
  type SkillSource,
  type SkillSummary,
  updateSkill,
} from "@/api/skills";
import { useAppStore } from "@/stores/app-store";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Badge } from "@/components/ui/badge";
import { SessionSidebar } from "@/components/navbar";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import {
  InputGroup,
  InputGroupAddon,
  InputGroupInput,
} from "@/components/ui/input-group";
import {
  Tabs,
  TabsList,
  TabsTrigger,
} from "@/components/ui/tabs";
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

const SOURCE_LABELS: Record<SkillSource, string> = {
  platform: "Platform",
  org: "Organization",
  user: "My skills",
};

const SOURCE_ORDER: SkillSource[] = ["user", "org", "platform"];

const EXPERT_STATUS_VARIANTS: Record<
  string,
  "default" | "secondary" | "outline" | "destructive"
> = {
  active: "default",
  collecting: "secondary",
  draft: "outline",
  retired: "destructive",
};

type TypeFilter = "all" | SkillKind;

export function SkillsPage() {
  const fetchUser = useAppStore((s) => s.fetchUser);
  const fetchSessions = useAppStore((s) => s.fetchSessions);

  const [skills, setSkills] = useState<SkillSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState("");
  const [typeFilter, setTypeFilter] = useState<TypeFilter>("all");

  const [selectedName, setSelectedName] = useState<string | null>(null);
  const [detail, setDetail] = useState<SkillDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  const [createOpen, setCreateOpen] = useState(false);
  const [editOpen, setEditOpen] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<SkillDetail | null>(null);

  useEffect(() => {
    void fetchSessions();
    void fetchUser();
  }, [fetchSessions, fetchUser]);

  const loadSkills = useCallback(async () => {
    setLoading(true);
    try {
      const response = await listSkills();
      setSkills(response.skills);
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadSkills();
  }, [loadSkills]);

  useEffect(() => {
    if (!selectedName) {
      setDetail(null);
      return;
    }
    setDetailLoading(true);
    let cancelled = false;
    getSkill(selectedName)
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
    return skills.filter((skill) => {
      if (typeFilter !== "all" && skill.type !== typeFilter) return false;
      if (!query) return true;
      const haystack = [
        skill.name,
        skill.description,
        skill.category ?? "",
        skill.trigger ?? "",
      ]
        .join(" ")
        .toLowerCase();
      return haystack.includes(query);
    });
  }, [skills, search, typeFilter]);

  const grouped = useMemo(() => {
    const buckets: Record<SkillSource, SkillSummary[]> = {
      user: [],
      org: [],
      platform: [],
    };
    for (const skill of filtered) {
      buckets[skill.source].push(skill);
    }
    return buckets;
  }, [filtered]);

  const isUserSkill = detail?.source === "user";

  const handleCreated = useCallback(
    async (name: string) => {
      setCreateOpen(false);
      await loadSkills();
      setSelectedName(name);
    },
    [loadSkills],
  );

  const handleEdited = useCallback(
    (name: string, content: string) => {
      setEditOpen(false);
      setDetail((prev) =>
        prev && prev.name === name ? { ...prev, content } : prev,
      );
      toast.success(`Skill '${name}' updated.`);
    },
    [],
  );

  const handleDelete = useCallback(async () => {
    if (!deleteTarget) return;
    try {
      await deleteSkill(deleteTarget.name);
      toast.success(`Skill '${deleteTarget.name}' deleted.`);
      setSkills((prev) => prev.filter((s) => s.name !== deleteTarget.name));
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
                Skills
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
                placeholder="Search skills..."
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
            </InputGroup>

            <Tabs
              value={typeFilter}
              onValueChange={(v) => setTypeFilter(v as TypeFilter)}
            >
              <TabsList variant="line" className="w-full">
                <TabsTrigger value="all">All</TabsTrigger>
                <TabsTrigger value="skill">Skills</TabsTrigger>
                <TabsTrigger value="expert">Experts</TabsTrigger>
              </TabsList>
            </Tabs>
          </header>

          <div className="flex-1 overflow-y-auto">
            {loading ? (
              <div className="flex items-center justify-center py-12 text-muted-foreground">
                <Loader2Icon className="w-4 h-4 animate-spin mr-2" />
                Loading...
              </div>
            ) : filtered.length === 0 ? (
              <div className="text-center py-12 px-4 space-y-2">
                <BookOpenIcon className="w-8 h-8 text-muted-foreground/40 mx-auto" />
                <p className="text-sm text-muted-foreground">
                  {search
                    ? "No skills match your search."
                    : "No skills available."}
                </p>
              </div>
            ) : (
              SOURCE_ORDER.map((source) => {
                const items = grouped[source];
                if (items.length === 0) return null;
                return (
                  <SkillGroup
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
              Loading skill...
            </div>
          ) : !detail ? (
            <EmptyState />
          ) : (
            <SkillDetailView
              detail={detail}
              isUserSkill={isUserSkill}
              onEdit={() => setEditOpen(true)}
              onDelete={() => setDeleteTarget(detail)}
            />
          )}
        </section>
      </main>

      <CreateSkillDialog
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        onCreated={handleCreated}
      />

      {detail && isUserSkill && (
        <EditSkillDialog
          open={editOpen}
          skill={detail}
          onClose={() => setEditOpen(false)}
          onEdited={handleEdited}
        />
      )}

      <ConfirmDialog
        open={deleteTarget !== null}
        title="Delete skill?"
        description={
          deleteTarget
            ? `This will permanently delete '${deleteTarget.name}' and all its supporting files. This cannot be undone.`
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

function SkillGroup({
  label,
  items,
  selectedName,
  onSelect,
}: {
  label: string;
  items: SkillSummary[];
  selectedName: string | null;
  onSelect: (name: string) => void;
}) {
  return (
    <div className="py-1">
      <div className="px-4 pt-3 pb-1 text-xs font-semibold uppercase tracking-wide text-faint">
        {label}
      </div>
      {items.map((skill) => {
        const isActive = skill.name === selectedName;
        return (
          <button
            type="button"
            key={skill.name}
            onClick={() => onSelect(skill.name)}
            className={cn(
              "w-full text-left px-4 py-2 border-l-2 transition-colors",
              isActive
                ? "bg-line text-foreground border-l-primary"
                : "border-l-transparent hover:bg-input text-subtle hover:text-foreground",
            )}
          >
            <div className="flex items-center gap-1.5 min-w-0">
              {skill.type === "expert" && (
                <SparklesIcon className="w-3 h-3 text-primary shrink-0" />
              )}
              <div className="font-medium text-sm truncate flex-1">
                {skill.name}
              </div>
              {skill.type === "expert" && skill.expert_status && (
                <Badge
                  variant={
                    EXPERT_STATUS_VARIANTS[skill.expert_status] ?? "outline"
                  }
                  className="text-[10px]"
                >
                  {skill.expert_status}
                </Badge>
              )}
            </div>
            {skill.description && (
              <div className="text-xs text-faint truncate mt-0.5">
                {skill.description}
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
      <FileTextIcon className="w-10 h-10 text-muted-foreground/40" />
      <div className="space-y-1">
        <p className="text-sm text-muted-foreground">Select a skill</p>
        <p className="text-sm text-faint max-w-sm">
          Skills are reusable prompts and capabilities. Platform skills are
          shipped with Surogates, organization skills are shared across your
          team, and your own skills are visible only to you.
        </p>
      </div>
    </div>
  );
}

function SkillDetailView({
  detail,
  isUserSkill,
  onEdit,
  onDelete,
}: {
  detail: SkillDetail;
  isUserSkill: boolean;
  onEdit: () => void;
  onDelete: () => void;
}) {
  return (
    <article className="max-w-3xl mx-auto px-8 py-8">
      <header className="mb-6 pb-6 border-b border-line">
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2 mb-2">
              {detail.type === "expert" ? (
                <SparklesIcon className="w-5 h-5 text-primary shrink-0" />
              ) : (
                <BookOpenIcon className="w-5 h-5 text-muted-foreground shrink-0" />
              )}
              <h2 className="text-2xl font-bold tracking-tight truncate">
                {detail.name}
              </h2>
            </div>
            {detail.description && (
              <p className="text-sm text-muted-foreground">
                {detail.description}
              </p>
            )}
          </div>

          {isUserSkill && (
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

      {detail.type === "expert" && <ExpertPanel detail={detail} />}

      {detail.linked_files.length > 0 && (
        <section className="mb-6">
          <h3 className="text-sm font-semibold uppercase tracking-wide text-faint mb-2">
            Linked files
          </h3>
          <ul className="space-y-1">
            {detail.linked_files.map((path) => (
              <li
                key={path}
                className="flex items-center gap-2 text-sm  text-subtle"
              >
                <FileTextIcon className="w-3.5 h-3.5 text-faint" />
                {path}
              </li>
            ))}
          </ul>
        </section>
      )}

      <section>
        <h3 className="text-sm font-semibold uppercase tracking-wide text-faint mb-2">
          Content
        </h3>
        <div className="prose prose-sm dark:prose-invert max-w-none">
          <MessageResponse>{detail.content}</MessageResponse>
        </div>
      </section>
    </article>
  );
}

function MetaRow({ detail }: { detail: SkillDetail }) {
  const items: { label: string; value: string }[] = [
    { label: "Source", value: SOURCE_LABELS[detail.source] },
  ];
  if (detail.category) items.push({ label: "Category", value: detail.category });
  if (detail.trigger) items.push({ label: "Trigger", value: detail.trigger });
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

function ExpertPanel({ detail }: { detail: SkillDetail }) {
  const status = detail.expert_status ?? "draft";
  const stats = detail.expert_stats ?? {};
  const totalUses = Number(stats.total_uses ?? 0);
  const totalSuccesses = Number(stats.total_successes ?? 0);
  const successRate =
    totalUses > 0 ? Math.round((totalSuccesses / totalUses) * 100) : null;

  return (
    <section className="mb-6 rounded-lg border border-line bg-card px-4 py-3 space-y-2">
      <div className="flex items-center gap-2">
        <SparklesIcon className="w-4 h-4 text-primary" />
        <h3 className="text-sm font-semibold text-foreground">
          Expert configuration
        </h3>
        <Badge
          variant={EXPERT_STATUS_VARIANTS[status] ?? "outline"}
          className="ml-auto"
        >
          {status}
        </Badge>
      </div>

      <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-sm">
        {detail.expert_model && (
          <>
            <dt className="text-faint">Base model</dt>
            <dd className="text-subtle  text-xs truncate">
              {detail.expert_model}
            </dd>
          </>
        )}
        {detail.expert_endpoint && (
          <>
            <dt className="text-faint">Endpoint</dt>
            <dd className="text-subtle  text-xs truncate">
              {detail.expert_endpoint}
            </dd>
          </>
        )}
        {detail.expert_max_iterations != null && (
          <>
            <dt className="text-faint">Max iterations</dt>
            <dd className="text-subtle">{detail.expert_max_iterations}</dd>
          </>
        )}
        {detail.expert_tools && detail.expert_tools.length > 0 && (
          <>
            <dt className="text-faint">Tools</dt>
            <dd className="text-subtle">{detail.expert_tools.join(", ")}</dd>
          </>
        )}
        {totalUses > 0 && (
          <>
            <dt className="text-faint">Usage</dt>
            <dd className="text-subtle">
              {totalSuccesses} / {totalUses} successful
              {successRate != null && ` (${successRate}%)`}
            </dd>
          </>
        )}
      </dl>
    </section>
  );
}

function CreateSkillDialog({
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
  const [content, setContent] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!open) {
      setName("");
      setCategory("");
      setContent("");
    }
  }, [open]);

  const handleSubmit = useCallback(async () => {
    if (!name.trim() || !content.trim()) return;
    setSaving(true);
    try {
      await createSkill({
        name: name.trim(),
        content,
        category: category.trim() || null,
      });
      toast.success(`Skill '${name}' created.`);
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
          <DialogTitle>New skill</DialogTitle>
          <DialogDescription>
            Create a skill visible only to you. Start with frontmatter
            containing <code>name</code> and <code>description</code>.
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
              <FieldLabel htmlFor="skill-name">Name</FieldLabel>
              <Input
                id="skill-name"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="my-skill"
                required
              />
            </Field>

            <Field orientation="vertical">
              <FieldLabel htmlFor="skill-category">Category (optional)</FieldLabel>
              <Input
                id="skill-category"
                value={category}
                onChange={(e) => setCategory(e.target.value)}
                placeholder="e.g. coding, research"
              />
            </Field>

            <Field orientation="vertical">
              <FieldLabel htmlFor="skill-content">SKILL.md content</FieldLabel>
              <Textarea
                id="skill-content"
                value={content}
                onChange={(e) => setContent(e.target.value)}
                rows={14}
                className=" text-xs"
                placeholder={
                  "---\nname: my-skill\ndescription: What this skill does\n---\n\n# Instructions\n\nDescribe what the agent should do..."
                }
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

function EditSkillDialog({
  open,
  skill,
  onClose,
  onEdited,
}: {
  open: boolean;
  skill: SkillDetail;
  onClose: () => void;
  onEdited: (name: string, content: string) => void;
}) {
  const [content, setContent] = useState(skill.content);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (open) setContent(skill.content);
  }, [open, skill.content]);

  const handleSubmit = useCallback(async () => {
    if (!content.trim()) return;
    setSaving(true);
    try {
      await updateSkill(skill.name, content);
      onEdited(skill.name, content);
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      setSaving(false);
    }
  }, [content, skill.name, onEdited]);

  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>Edit '{skill.name}'</DialogTitle>
          <DialogDescription>
            Replace the full SKILL.md content. Frontmatter must stay valid.
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
              <FieldLabel htmlFor="skill-edit-content">
                SKILL.md content
              </FieldLabel>
              <Textarea
                id="skill-edit-content"
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
