import { Button } from "@invergent/ui-sdk/button";
import { TonePill } from "@invergent/ui-sdk/chip";
import { CONTROL_FIELD } from "@invergent/ui-sdk/control-styles";
import { SegmentedControl } from "@invergent/ui-sdk/segmented-control";
import { TextLink } from "@invergent/ui-sdk/text-link";
import { ArrowLeftIcon, Loader2Icon } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { cn } from "../../lib/utils";
import type { AgentChatAdapter, CodingAgentConnection } from "../../types";

export interface CodingAgentsPanelProps {
  agentId?: string;
  adapter: AgentChatAdapter;
  /** Optional. When omitted (e.g. embedded as a settings tab) the
   *  "Back" affordance is not rendered. */
  onBack?: () => void;
}

type Mode = "oauth" | "api_key";

const MODES = [
  { value: "oauth", label: "Subscription" },
  { value: "api_key", label: "API key" },
] as const;

interface ProviderMeta {
  /** Slug used in the "/code" commands (claude/codex). */
  key: string;
  /** Provider id the backend keys connections by. */
  provider: CodingAgentConnection["provider"];
  name: string;
  /** Subscription/OAuth paste instructions. */
  oauthInstructions: string;
  /** API-key paste instructions. */
  apiKeyInstructions: string;
  /** OAuth-mode placeholder for the paste box. */
  oauthPlaceholder: string;
  /** API-key-mode placeholder for the paste box. */
  apiKeyPlaceholder: string;
  /**
   * Client-side validation of an OAuth-mode value. Returns an error string
   * (shown inline) or null when the value looks acceptable.
   */
  validateOauth: (value: string) => string | null;
}

const PROVIDERS: ProviderMeta[] = [
  {
    key: "claude",
    provider: "anthropic",
    name: "Claude Code",
    oauthInstructions:
      "Run `claude setup-token` locally, then paste the `sk-ant-oat...` token it prints to connect your Claude subscription.",
    apiKeyInstructions:
      "Paste an Anthropic API key (`sk-ant-...`) to bill usage to your API account instead of a subscription.",
    oauthPlaceholder: "sk-ant-oat...",
    apiKeyPlaceholder: "sk-ant-...",
    validateOauth: (value) =>
      value.startsWith("sk-ant-oat")
        ? null
        : "That doesn't look like a setup token. It should start with `sk-ant-oat`.",
  },
  {
    key: "codex",
    provider: "openai",
    name: "Codex",
    oauthInstructions:
      "Run `codex login` locally, then paste the contents of `~/.codex/auth.json` to connect your ChatGPT plan.",
    apiKeyInstructions:
      "Paste an OpenAI API key (`sk-...`) to bill usage to your API account instead of a ChatGPT plan.",
    oauthPlaceholder: '{"OPENAI_API_KEY": ..., "tokens": { ... }}',
    apiKeyPlaceholder: "sk-...",
    validateOauth: (value) => {
      try {
        JSON.parse(value);
        return null;
      } catch {
        return "That doesn't look like valid JSON. Paste the whole `~/.codex/auth.json` file.";
      }
    },
  },
];

/**
 * "Connect your coding plan" view. One card per provider (Claude / Codex)
 * with a subscription-vs-API-key toggle and a masked paste box. Mirrors the
 * IntegrationsPage shell. Renders nothing if the adapter doesn't expose the
 * coding-agent methods.
 */
export function CodingAgentsPanel({ agentId, adapter, onBack }: CodingAgentsPanelProps) {
  const supported =
    !!adapter.listCodingAgentConnections &&
    !!adapter.submitCodingAgentCredential &&
    !!adapter.disconnectCodingAgentProvider;

  const [connections, setConnections] = useState<CodingAgentConnection[]>([]);

  const refresh = useCallback(async () => {
    if (!adapter.listCodingAgentConnections) return;
    const res = await adapter.listCodingAgentConnections({ agentId });
    setConnections(res.connections);
  }, [adapter, agentId]);

  useEffect(() => {
    if (!supported) return;
    void refresh();
  }, [supported, refresh]);

  const byProvider = useMemo(() => {
    const map = new Map<string, CodingAgentConnection>();
    for (const c of connections) map.set(c.provider, c);
    return map;
  }, [connections]);

  if (!supported) return null;

  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col gap-6 p-6">
      {onBack && (
        <TextLink asChild variant="muted">
          <button type="button" onClick={onBack}>
            <ArrowLeftIcon className="size-4" />
            Back
          </button>
        </TextLink>
      )}
      <div>
        <h1 className="text-2xl font-bold text-foreground">Coding agents</h1>
        <p className="text-sm text-muted-foreground">
          Connect your Claude or ChatGPT plan so the agent can run `/code` on
          your behalf without separate API billing.
        </p>
      </div>
      <div className="flex flex-col gap-4">
        {PROVIDERS.map((meta) => (
          <ProviderCard
            key={meta.key}
            meta={meta}
            connection={byProvider.get(meta.provider) ?? null}
            adapter={adapter}
            agentId={agentId}
            onChanged={refresh}
          />
        ))}
      </div>
    </div>
  );
}

function ProviderCard({
  meta,
  connection,
  adapter,
  agentId,
  onChanged,
}: {
  meta: ProviderMeta;
  connection: CodingAgentConnection | null;
  adapter: AgentChatAdapter;
  agentId?: string;
  onChanged: () => Promise<void>;
}) {
  const [mode, setMode] = useState<Mode>("oauth");
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const connected = connection?.connected ?? false;
  const instructions =
    mode === "oauth" ? meta.oauthInstructions : meta.apiKeyInstructions;
  const placeholder =
    mode === "oauth" ? meta.oauthPlaceholder : meta.apiKeyPlaceholder;

  const submit = async () => {
    if (!adapter.submitCodingAgentCredential) return;
    const trimmed = value.trim();
    setError(null);
    if (!trimmed) {
      setError("Paste a value first.");
      return;
    }
    if (mode === "oauth") {
      const hint = meta.validateOauth(trimmed);
      if (hint) {
        setError(hint);
        return;
      }
    }
    setBusy(true);
    try {
      await adapter.submitCodingAgentCredential({
        agentId,
        provider: meta.provider,
        mode,
        value: trimmed,
      });
      setValue("");
      await onChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to save the credential");
    } finally {
      setBusy(false);
    }
  };

  const disconnect = async () => {
    if (!adapter.disconnectCodingAgentProvider) return;
    setBusy(true);
    setError(null);
    try {
      await adapter.disconnectCodingAgentProvider({ agentId, provider: meta.provider });
      await onChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to disconnect");
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="flex flex-col gap-3 rounded-lg border border-border p-4">
      <div className="flex items-center justify-between gap-2">
        <h2 className="text-base font-semibold text-foreground">{meta.name}</h2>
        <TonePill tone={connected ? "ok" : "muted"}>
          {connected
            ? `Connected${
                connection?.auth_mode === "api_key" ? " (API key)" : ""
              }`
            : "Not connected"}
        </TonePill>
      </div>

      <SegmentedControl
        aria-label="Credential type"
        segments={MODES}
        value={mode}
        onChange={(next) => {
          setMode(next);
          setError(null);
        }}
        className="self-start"
      />

      <p className="text-sm text-muted-foreground">{instructions}</p>

      <textarea
        value={value}
        onChange={(e) => setValue(e.target.value)}
        placeholder={placeholder}
        spellCheck={false}
        autoComplete="off"
        rows={mode === "oauth" && meta.provider === "openai" ? 4 : 2}
        aria-label={`${meta.name} credential`}
        className={cn(CONTROL_FIELD, "resize-y font-mono [-webkit-text-security:disc]")}
      />

      {error && (
        <div role="alert" className="text-xs text-red-500">
          {error}
        </div>
      )}

      <div className="flex items-center gap-2">
        <Button
          type="button"
          variant="outline"
          size="xs"
          disabled={busy}
          onClick={submit}
        >
          {busy && <Loader2Icon className="h-3.5 w-3.5 animate-spin" />}
          {connected ? "Update" : "Connect"}
        </Button>
        {connected && (
          <Button
            type="button"
            variant="outline"
            size="xs"
            disabled={busy}
            onClick={disconnect}
          >
            Disconnect
          </Button>
        )}
      </div>
    </section>
  );
}
