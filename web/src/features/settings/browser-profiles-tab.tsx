import { useCallback, useEffect, useState } from "react";
import { Globe, Play, Plus, Trash2 } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import {
  type BrowserProfile,
  createBrowserProfile,
  deleteBrowserProfile,
  listBrowserProfiles,
} from "@/api/browser-profiles";

import { BrowserProfileCreateDialog } from "./browser-profile-create-dialog";
import { BrowserProfileSetupDialog } from "./browser-profile-setup-dialog";

function formatDate(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleDateString();
}

export function BrowserProfilesTab() {
  const [profiles, setProfiles] = useState<BrowserProfile[]>([]);
  const [loading, setLoading] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [removeId, setRemoveId] = useState<string | null>(null);
  const [setupId, setSetupId] = useState<string | null>(null);

  const refresh = useCallback(() => {
    setLoading(true);
    listBrowserProfiles()
      .then(setProfiles)
      .catch(() => toast.error("Couldn't load browser profiles."))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  async function handleCreate(name: string) {
    try {
      await createBrowserProfile(name || `Profile ${profiles.length + 1}`);
      refresh();
    } catch {
      toast.error("Couldn't create profile — that name may already exist.");
    }
  }

  async function handleDelete() {
    if (!removeId) return;
    try {
      await deleteBrowserProfile(removeId);
      setRemoveId(null);
      refresh();
    } catch {
      toast.error("Couldn't delete profile.");
    }
  }

  return (
    <div>
      {/* Title, explanation and action do not fit one line on a phone; the
          action takes its own row there rather than squeezing the copy. */}
      <div className="mb-2 flex flex-col items-stretch gap-3 sm:flex-row sm:items-center sm:justify-between sm:gap-4">
        <div className="min-w-0">
          <h2 className="text-base font-semibold text-foreground">
            Browser Profiles
          </h2>
          <p className="text-sm text-muted-foreground">
            Preserve browser state and login sessions across tasks.
          </p>
        </div>
        <Button
          size="sm"
          onClick={() => setCreateOpen(true)}
          className="shrink-0"
        >
          <Plus className="size-4" /> Create Profile
        </Button>
      </div>

      {loading && profiles.length === 0 ? (
        <div className="text-sm text-muted-foreground">Loading…</div>
      ) : profiles.length === 0 ? (
        <div className="text-sm text-muted-foreground">No profiles yet.</div>
      ) : (
        <div className="space-y-3 mt-4">
          {profiles.map((p) => (
            <div
              key={p.id}
              className="bg-card border border-line rounded-xl px-5 py-4"
            >
              {/* "Set up authentication" is a wide button; beside a profile
                  name on a phone neither has room, so the row wraps. */}
              <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-2">
                <div className="min-w-0 text-sm font-semibold text-foreground">
                  {p.name}
                </div>
                <div className="ml-auto flex shrink-0 items-center gap-2">
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => setSetupId(p.id)}
                  >
                    <Play className="size-3.5" /> Set up authentication
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => setRemoveId(p.id)}
                    aria-label="Delete profile"
                  >
                    <Trash2 className="size-3.5 text-destructive" />
                  </Button>
                </div>
              </div>
              <div className="text-xs text-muted-foreground mt-2">
                Created {formatDate(p.createdAt)}
                {p.lastUsedAt && ` · Last used ${formatDate(p.lastUsedAt)}`}
              </div>
              {p.cookieDomains.length > 0 && (
                <div className="flex items-center gap-1.5 mt-2 text-xs text-muted-foreground">
                  <Globe className="size-3.5" />
                  Cookie Domains ({p.cookieDomains.length}):{" "}
                  {p.cookieDomains.join(", ")}
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      <BrowserProfileCreateDialog
        open={createOpen}
        onOpenChange={setCreateOpen}
        onCreate={handleCreate}
      />

      <ConfirmDialog
        open={!!removeId}
        title="Delete this profile?"
        description="Its saved authentication will be permanently removed."
        confirmLabel="Delete"
        onConfirm={handleDelete}
        onCancel={() => setRemoveId(null)}
      />

      {setupId && (
        <BrowserProfileSetupDialog
          profileId={setupId}
          open={!!setupId}
          onOpenChange={(o) => {
            if (!o) setSetupId(null);
          }}
          onSaved={() => {
            setSetupId(null);
            refresh();
          }}
        />
      )}
    </div>
  );
}
