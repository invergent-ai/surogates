import { useEffect, useId, useRef, useState } from "react";
import { useTheme } from "next-themes";
import { Button } from "@invergent/ui-sdk/button";
import { AlertCircleIcon, DownloadIcon, FileTextIcon, Maximize2Icon, Minimize2Icon, XIcon } from "lucide-react";
import { type DocEditorEvents, type DocEditorInstance, loadDocsApi, type MentionNotice } from "../../lib/office";
import type { AgentChatAdapter, AgentChatOfficeEvent } from "../../types";

interface OfficeEditorPaneProps {
	adapter: AgentChatAdapter;
	sessionId: string;
	path: string;
	view: boolean;
	/** The latest save trouble reported for this file. */
	officeEvent?: AgentChatOfficeEvent;
	onClose: () => void;
}

/** What the pane says about a save, or null when there is nothing to say. */
function saveNotice(notice: AgentChatOfficeEvent): string | null {
	const computer = notice.computer ?? "your computer";
	if (notice.type === "office.conflict") {
		return `This document changed outside the editor. Your edits were saved to ${notice.conflictPath}.`;
	}
	if (notice.type === "office.saved") return null;
	if (notice.type === "office.save_waiting") {
		return notice.reason === "offline"
			? `Saving to ${computer} once it is back online.`
			: `Saving to ${computer}… If it asks, allow the save there.`;
	}
	if (notice.reason === "declined") {
		return `${notice.computer ?? "Your computer"} declined the save. Your changes are still here; the next save asks again.`;
	}
	return "Your latest changes could not be saved yet.";
}

/** An office document open in the document server's editor, in the file pane. */
export function OfficeEditorPane({ adapter, sessionId, path, view, officeEvent, onClose }: OfficeEditorPaneProps) {
	const { resolvedTheme } = useTheme();
	const theme = resolvedTheme === "dark" ? "dark" : "light";
	const placeholderId = `office-editor-${useId().replace(/[^a-zA-Z0-9_-]/g, "")}`;
	const [error, setError] = useState<string | null>(null);
	const [loading, setLoading] = useState(true);
	const [mentionTrouble, setMentionTrouble] = useState<string | null>(null);
	// Trouble reported before the pane opened is old news: the editor opens the file as it stands.
	const [openedAfter] = useState(() => officeEvent?.eventId ?? 0);
	const notice = officeEvent && officeEvent.eventId > openedAfter ? officeEvent : null;
	// A conflict ended the editor's session: open the file again, with a fresh config.
	const [reopenedFor, setReopenedFor] = useState(0);
	if (notice?.type === "office.conflict" && notice.eventId !== reopenedFor) setReopenedFor(notice.eventId);
	// The browser's full screen: Esc leaves it too, so the state follows the document's.
	const paneRef = useRef<HTMLElement>(null);
	const [fullScreen, setFullScreen] = useState(false);
	useEffect(() => {
		const follow = () => setFullScreen(document.fullscreenElement === paneRef.current);
		document.addEventListener("fullscreenchange", follow);
		return () => document.removeEventListener("fullscreenchange", follow);
	}, []);

	useEffect(() => {
		let cancelled = false;
		let editor: DocEditorInstance | null = null;
		setError(null);
		setLoading(true);
		setMentionTrouble(null);
		void (async () => {
			try {
				if (!adapter.getWorkspaceEditorConfig) throw new Error("Preview not available. Download the file to open it.");
				const opened = await adapter.getWorkspaceEditorConfig({ sessionId, path, theme, view });
				await loadDocsApi(opened.document_server_url);
				if (cancelled) return;
				if (!window.DocsAPI) throw new Error("The document editor could not be loaded.");
				const users = opened.mentionable_users ?? [];
				const notify = adapter.notifyWorkspaceEditorMention?.bind(adapter);
				// The chat's agent is the one person a comment can mention. A comment that mentions it is sent to this
				// chat, whose agent answers in the comment's thread.
				const events: DocEditorEvents | null = users.length && notify ? {
					onRequestUsers: (event) => editor?.setUsers({ c: (event.data as { c?: unknown } | undefined)?.c, users }),
					onRequestSendNotify: (event) => {
						const mention = (event.data ?? {}) as MentionNotice;
						const emails = mention.emails ?? [];
						const named = new Set(emails.map((email) => email.toLowerCase()));
						if (!users.some((user) => named.has(user.email.toLowerCase()))) return;
						setMentionTrouble(null);
						void notify({
							sessionId, path, emails, message: mention.message ?? "",
							commentId: String(mention.actionLink?.action?.data ?? mention.actionId ?? ""),
						}).catch((sendError: unknown) => {
							if (!cancelled) setMentionTrouble(`The agent was not told about your comment: ${(sendError as Error).message}`);
						});
					},
				} : null;
				editor = new window.DocsAPI.DocEditor(placeholderId, events ? { ...opened.config, events } : opened.config);
			} catch (openError) {
				if (!cancelled) setError((openError as Error).message);
			} finally {
				if (!cancelled) setLoading(false);
			}
		})();
		return () => {
			cancelled = true;
			editor?.destroyEditor();
		};
	}, [adapter, sessionId, path, theme, view, placeholderId, reopenedFor]);

	const fileName = path.split("/").pop() ?? path;
	return (
		<aside
			ref={paneRef}
			data-testid="file-preview-panel"
			className="flex h-full min-h-0 w-full flex-col overflow-hidden border-l border-line bg-card"
		>
			<div className="flex shrink-0 items-center gap-2 border-b border-line px-3 py-2">
				<FileTextIcon className="size-4 shrink-0 text-muted-foreground" />
				<div className="min-w-0 flex-1 truncate font-medium">{fileName}</div>
				<Button asChild variant="ghost" size="icon-xs">
					<a
						href={adapter.getWorkspaceDownloadUrl({ sessionId, path })}
						download={fileName}
						aria-label={`Download ${fileName}`}
						title="Download"
					>
						<DownloadIcon className="size-4" />
					</a>
				</Button>
				<Button
					type="button"
					variant="ghost"
					size="icon-xs"
					onClick={() => void (fullScreen ? document.exitFullscreen() : paneRef.current?.requestFullscreen())}
					aria-label={fullScreen ? "Exit full screen" : "Full screen"}
					title={fullScreen ? "Exit full screen" : "Full screen"}
				>
					{fullScreen ? <Minimize2Icon className="size-4" /> : <Maximize2Icon className="size-4" />}
				</Button>
				<Button type="button" variant="ghost" size="icon-xs" onClick={onClose} aria-label="Close file">
					<XIcon className="size-4" />
				</Button>
			</div>
			{error && (
				<div role="alert" className="flex items-start gap-2 p-3 text-xs text-destructive">
					<AlertCircleIcon className="mt-0.5 size-3.5 shrink-0" />
					<span>{error}</span>
				</div>
			)}
			{notice && saveNotice(notice) && (
				<div role="status" className="border-b border-line px-3 py-2 text-xs text-muted-foreground">
					{saveNotice(notice)}
				</div>
			)}
			{mentionTrouble && (
				<div role="status" className="border-b border-line px-3 py-2 text-xs text-muted-foreground">
					{mentionTrouble}
				</div>
			)}
			{loading && !error && <div className="p-3 text-sm text-muted-foreground">Opening {fileName}…</div>}
			<div className="min-h-0 flex-1">
				<div id={placeholderId} />
			</div>
		</aside>
	);
}
