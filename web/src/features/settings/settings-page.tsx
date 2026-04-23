// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "@tanstack/react-router";
import {
  SaveIcon,
  Loader2Icon,
  TrashIcon,
  LinkIcon,
} from "lucide-react";
import { toast } from "sonner";
import { useAppStore } from "@/stores/app-store";
import {
  updateCurrentUser,
  fetchMyChannels,
  unlinkChannel,
  type ChannelIdentity,
} from "@/api/auth";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { SessionSidebar } from "@/components/navbar";
import {
  Tabs,
  TabsList,
  TabsTrigger,
  TabsContent,
} from "@/components/ui/tabs";
import {
  Field,
  FieldGroup,
  FieldLabel,
} from "@/components/ui/field";
import {
  Table,
  TableHeader,
  TableBody,
  TableHead,
  TableRow,
  TableCell,
} from "@/components/ui/table";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Badge } from "@/components/ui/badge";

const PLATFORM_LABELS: Record<string, string> = {
  slack: "Slack",
  teams: "Microsoft Teams",
  telegram: "Telegram",
};

export function SettingsPage() {
  const navigate = useNavigate();
  const user = useAppStore((s) => s.user);
  const fetchUser = useAppStore((s) => s.fetchUser);
  const fetchSessions = useAppStore((s) => s.fetchSessions);

  // Load sidebar data.
  useEffect(() => {
    void fetchSessions();
    void fetchUser();
  }, [fetchSessions, fetchUser]);

  // ── Profile tab state ──────────────────────────────────────────────

  const [displayName, setDisplayName] = useState("");
  const [email, setEmail] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (user) {
      setDisplayName(user.display_name ?? "");
      setEmail(user.email);
    }
  }, [user]);

  const dirty =
    user != null &&
    (displayName !== (user.display_name ?? "") || email !== user.email);

  const handleSave = useCallback(async () => {
    if (!dirty) return;
    setSaving(true);
    try {
      await updateCurrentUser({ display_name: displayName, email });
      await fetchUser();
      toast.success("Profile updated.");
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      setSaving(false);
    }
  }, [dirty, displayName, email, fetchUser]);

  // ── Channels tab state ─────────────────────────────────────────────

  const [channels, setChannels] = useState<ChannelIdentity[]>([]);
  const [channelsLoading, setChannelsLoading] = useState(false);
  const [unlinkTarget, setUnlinkTarget] = useState<ChannelIdentity | null>(
    null,
  );

  const loadChannels = useCallback(async () => {
    setChannelsLoading(true);
    try {
      setChannels(await fetchMyChannels());
    } catch {
      toast.error("Failed to load connected channels.");
    } finally {
      setChannelsLoading(false);
    }
  }, []);

  const handleUnlink = useCallback(async () => {
    if (!unlinkTarget) return;
    try {
      await unlinkChannel(unlinkTarget.id);
      setChannels((prev) => prev.filter((c) => c.id !== unlinkTarget.id));
      toast.success(
        `${PLATFORM_LABELS[unlinkTarget.platform] ?? unlinkTarget.platform} account unlinked.`,
      );
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      setUnlinkTarget(null);
    }
  }, [unlinkTarget]);

  return (
    <>
      <SessionSidebar />
      <main className="flex-1 overflow-y-auto">
        <div className="max-w-2xl mx-auto px-6 py-10">
          {/* Header */}
          <div className="flex items-center gap-3 mb-8">
            <h1 className="text-xl font-bold tracking-tight text-foreground">
              Settings
            </h1>
          </div>

          <Tabs defaultValue="profile">
            <TabsList variant="line" className="mb-6">
              <TabsTrigger value="profile">Profile</TabsTrigger>
              <TabsTrigger
                value="channels"
                onClick={() => {
                  if (channels.length === 0) void loadChannels();
                }}
              >
                Connected Channels
              </TabsTrigger>
            </TabsList>

            {/* ── Profile ── */}
            <TabsContent value="profile">
              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  void handleSave();
                }}
              >
                <FieldGroup>
                  <Field orientation="vertical">
                    <FieldLabel htmlFor="display-name">Display name</FieldLabel>
                    <Input
                      id="display-name"
                      value={displayName}
                      onChange={(e) => setDisplayName(e.target.value)}
                      placeholder="Your name"
                    />
                  </Field>

                  <Field orientation="vertical">
                    <FieldLabel htmlFor="email">Email</FieldLabel>
                    <Input
                      id="email"
                      type="email"
                      value={email}
                      onChange={(e) => setEmail(e.target.value)}
                      placeholder="you@example.com"
                    />
                  </Field>

                  {user && (
                    <div className="pt-2 space-y-1 text-sm text-muted-foreground">
                      <div>
                        <span className="text-subtle font-medium">
                          Auth provider:
                        </span>{" "}
                        {user.auth_provider}
                      </div>
                      <div>
                        <span className="text-subtle font-medium">
                          Member since:
                        </span>{" "}
                        {new Date(user.created_at).toLocaleDateString()}
                      </div>
                    </div>
                  )}
                </FieldGroup>

                <div className="mt-6">
                  <Button
                    type="submit"
                    disabled={!dirty || saving}
                    className={cn("gap-2", !dirty && "opacity-50")}
                  >
                    {saving ? (
                      <Loader2Icon className="w-4 h-4 animate-spin" />
                    ) : (
                      <SaveIcon className="w-4 h-4" />
                    )}
                    Save changes
                  </Button>
                </div>
              </form>
            </TabsContent>

            {/* ── Connected Channels ── */}
            <TabsContent value="channels">
              {channelsLoading ? (
                <div className="flex items-center justify-center py-12 text-muted-foreground">
                  <Loader2Icon className="w-4 h-4 animate-spin mr-2" />
                  Loading...
                </div>
              ) : channels.length === 0 ? (
                <div className="text-center py-12 space-y-3">
                  <LinkIcon className="w-8 h-8 text-muted-foreground/40 mx-auto" />
                  <p className="text-sm text-muted-foreground">
                    No connected channels yet.
                  </p>
                  <p className="text-sm text-faint">
                    Use a pairing code from Slack, Teams, or Telegram to link
                    your account.
                  </p>
                  <Button
                    variant="outline"
                    onClick={() => void navigate({ to: "/link" })}
                  >
                    Link a channel
                  </Button>
                </div>
              ) : (
                <>
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>Platform</TableHead>
                        <TableHead>User ID</TableHead>
                        <TableHead className="w-0" />
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {channels.map((ch) => (
                        <TableRow key={ch.id}>
                          <TableCell>
                            <Badge variant="default" className="text-sm">
                              {PLATFORM_LABELS[ch.platform] ?? ch.platform}
                            </Badge>
                          </TableCell>
                          <TableCell className="">
                            {ch.platform_user_id}
                          </TableCell>
                          <TableCell>
                            <Button
                              variant="ghost"
                              onClick={() => setUnlinkTarget(ch)}
                              className="text-muted-foreground hover:text-destructive"
                            >
                              <TrashIcon className="w-5 h-5" />
                            </Button>
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>

                  <div className="mt-4">
                    <Button
                      variant="outline"
                      onClick={() => void navigate({ to: "/link" })}
                    >
                      Link another channel
                    </Button>
                  </div>
                </>
              )}

              <ConfirmDialog
                open={unlinkTarget !== null}
                title="Unlink channel?"
                description={
                  unlinkTarget
                    ? `This will disconnect your ${PLATFORM_LABELS[unlinkTarget.platform] ?? unlinkTarget.platform} account (${unlinkTarget.platform_user_id}). You will need a new pairing code to re-link.`
                    : ""
                }
                confirmLabel="Unlink"
                variant="destructive"
                onConfirm={handleUnlink}
                onCancel={() => setUnlinkTarget(null)}
              />
            </TabsContent>
          </Tabs>
        </div>
      </main>
    </>
  );
}
