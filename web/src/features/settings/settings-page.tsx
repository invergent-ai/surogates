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
import { CodingAgentsPanel } from "@invergent/agent-chat-react";
import { surogatesWebChatAdapter } from "@/features/chat";
import { BrowserProfilesTab } from "./browser-profiles-tab";
import { PlanTokensTab } from "./plan-tokens-tab";
import { PasswordSection } from "./password-section";
import { useAppStore } from "@/stores/app-store";
import {
  browserCapabilityEnabled,
  slashCommandEnabled,
} from "@/stores/capabilities-slice";
import {
  updateCurrentUser,
  fetchMyChannels,
  unlinkChannel,
  type ChannelIdentity,
} from "@/api/auth";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { AppShell } from "@/components/app-shell";
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
  const fetchCapabilities = useAppStore((s) => s.fetchCapabilities);
  const slashCommands = useAppStore((s) => s.slashCommands);
  const browserEnabled = useAppStore((s) => s.browserEnabled);
  const linkableChannels = useAppStore((s) => s.linkableChannels);

  // Only surface a capability tab the agent actually offers.  Unknown
  // (capabilities not yet loaded) fails open via the helpers.
  const codingAgentsEnabled = slashCommandEnabled(slashCommands, "code");
  const browserProfilesEnabled = browserCapabilityEnabled(browserEnabled);
  // Connected Channels only makes sense when the agent has a messaging
  // channel to pair against. An empty (loaded) list hides it; ``null``
  // (not yet loaded / older backend) also hides it, since we cannot
  // claim a channel exists.
  const channelsEnabled = (linkableChannels?.length ?? 0) > 0;

  // Load sidebar + capability data.
  useEffect(() => {
    void fetchSessions();
    void fetchUser();
    void fetchCapabilities();
  }, [fetchSessions, fetchUser, fetchCapabilities]);

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

  // Email is read-only (login identity), so only the display name is
  // editable here.
  const dirty = user != null && displayName !== (user.display_name ?? "");

  const handleSave = useCallback(async () => {
    if (!dirty) return;
    setSaving(true);
    try {
      await updateCurrentUser({ display_name: displayName });
      await fetchUser();
      toast.success("Profile updated.");
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      setSaving(false);
    }
  }, [dirty, displayName, fetchUser]);

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
    <AppShell sidebar={<SessionSidebar />}>
      <div className="flex-1 overflow-y-auto">
        <div className="max-w-2xl mx-auto px-4 sm:px-6 py-6 sm:py-10">
          {/* Header */}
          <div className="flex items-center gap-3 mb-8">
            <h1 className="text-xl font-bold tracking-tight text-foreground">
              Settings
            </h1>
          </div>

          <Tabs defaultValue="profile">
            <TabsList variant="line" className="mb-6 overflow-x-auto">
              <TabsTrigger value="profile">Profile</TabsTrigger>
              {channelsEnabled && (
                <TabsTrigger
                  value="channels"
                  onClick={() => {
                    if (channels.length === 0) void loadChannels();
                  }}
                >
                  Connected Channels
                </TabsTrigger>
              )}
              <TabsTrigger value="plan">Plan &amp; Tokens</TabsTrigger>
              {codingAgentsEnabled && (
                <TabsTrigger value="coding-agents">Coding Agents</TabsTrigger>
              )}
              {browserProfilesEnabled && (
                <TabsTrigger value="browser-profiles">
                  Browser Profiles
                </TabsTrigger>
              )}
            </TabsList>

            {/* ── Plan & tokens ── */}
            <TabsContent value="plan">
              <PlanTokensTab />
            </TabsContent>

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
                      readOnly
                      disabled
                      className="cursor-not-allowed opacity-70"
                    />
                    <p className="text-sm text-muted-foreground">
                      Your sign-in email cannot be changed here.
                    </p>
                  </Field>

                  {user && (
                    <div className="pt-2 text-sm text-muted-foreground">
                      <span className="text-subtle font-medium">
                        Member since:
                      </span>{" "}
                      {new Date(user.created_at).toLocaleDateString()}
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

              {user && (
                <PasswordSection
                  authProvider={user.auth_provider}
                  signInProvider={user.sign_in_provider}
                  email={user.email}
                />
              )}
            </TabsContent>

            {/* ── Connected Channels ── */}
            {channelsEnabled && (
            <TabsContent value="channels">
              <p className="mb-6 text-sm text-muted-foreground leading-relaxed">
                Connect the Slack, Teams, or Telegram account you also use to
                message this assistant, so it knows that chat is you.
              </p>
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
            )}

            {/* ── Coding Agents ── */}
            {codingAgentsEnabled && (
              <TabsContent value="coding-agents">
                <CodingAgentsPanel adapter={surogatesWebChatAdapter} />
              </TabsContent>
            )}

            {browserProfilesEnabled && (
              <TabsContent value="browser-profiles">
                <BrowserProfilesTab />
              </TabsContent>
            )}
          </Tabs>
        </div>
      </div>
    </AppShell>
  );
}
