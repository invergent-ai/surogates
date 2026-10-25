import { useEffect, useRef, useState } from "react";
import { Button } from "@invergent/ui-sdk/button";
import { DownloadIcon, FileTextIcon, Maximize2Icon, Minimize2Icon, XIcon } from "lucide-react";
import type { DocEditorEvents, DocEditorInstance, MentionNotice } from "../../lib/office";
import type { AgentChatAdapter, AgentChatOfficeEvent } from "../../types";
import { DocumentEditor, type EditorOpening } from "./document-editor";

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
	const [mentionTrouble, setMentionTrouble] = useState<string | null>(null);
	// Trouble reported before the pane opened is old news: the editor opens the file as it stands.
	const [openedAfter] = useState(() => officeEvent?.eventId ?? 0);
	const notice = officeEvent && officeEvent.eventId > openedAfter ? officeEvent : null;
	// A conflict ended the editor's session: open the file again, with a fresh config.
	const [reopenedFor, setReopenedFor] = useState(0);
	if (notice?.type === "office.conflict" && notice.eventId !== reopenedFor) setReopenedFor(notice.eventId);
	// Another adapter, chat, file or access opens the document again, as a conflict does.
	const openedFor = [adapter, sessionId, path, view, reopenedFor] as const;
	const [opens, setOpens] = useState({ for: openedFor, count: 0 });
	if (openedFor.some((value, at) => value !== opens.for[at])) setOpens({ for: openedFor, count: opens.count + 1 });
	// The browser's full screen: Esc leaves it too, so the state follows the document's.
	const paneRef = useRef<HTMLElement>(null);
	const [fullScreen, setFullScreen] = useState(false);
	useEffect(() => {
		const follow = () => setFullScreen(document.fullscreenElement === paneRef.current);
		document.addEventListener("fullscreenchange", follow);
		return () => document.removeEventListener("fullscreenchange", follow);
	}, []);

	const open = async (theme: "light" | "dark") => {
		// Each opening starts without the trouble an earlier one had.
		setMentionTrouble(null);
		if (!adapter.getWorkspaceEditorConfig) throw new Error("Preview not available. Download the file to open it.");
		return adapter.getWorkspaceEditorConfig({ sessionId, path, theme, view });
	};

	const events = (editor: () => DocEditorInstance | null, opened: EditorOpening): Partial<DocEditorEvents> | null => {
		const users = opened.mentionable_users ?? [];
		const notify = adapter.notifyWorkspaceEditorMention?.bind(adapter);
		// The chat's agent is the one person a comment can mention. A comment that mentions it is sent to this
		// chat, whose agent answers in the comment's thread. Any other list the editor asks for (a sheet's
		// protected range) gets nobody: the agent's mention id is no editor's, and a right given to it grants none.
		if (!users.length || !notify) return null;
		return {
			onRequestUsers: (event) => {
				const c = (event.data as { c?: unknown } | undefined)?.c;
				editor()?.setUsers({ c, users: c === "mention" || c === "info" ? users : [] });
			},
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
					// A closed editor's trouble is old news.
					if (editor()) setMentionTrouble(`The agent was not told about your comment: ${(sendError as Error).message}`);
				});
			},
		};
	};

	const fileName = path.split("/").pop() ?? path;
	const downloadUrl = adapter.getWorkspaceDownloadUrl({ sessionId, path });
	return (
		<aside
			ref={paneRef}
			data-testid="file-preview-panel"
			className="flex h-full min-h-0 w-full flex-col overflow-hidden border-l border-line bg-card"
		>
			<div className="flex shrink-0 items-center gap-2 border-b border-line px-3 py-2">
				<FileTextIcon className="size-4 shrink-0 text-muted-foreground" />
				<div className="min-w-0 flex-1 truncate font-medium">{fileName}</div>
				{/* An adapter with no URL for the file offers no download. */}
				{downloadUrl && (
					<Button asChild variant="ghost" size="icon-xs">
						<a
							href={downloadUrl}
							download={fileName}
							aria-label={`Download ${fileName}`}
							title="Download"
						>
							<DownloadIcon className="size-4" />
						</a>
					</Button>
				)}
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
			<DocumentEditor
				open={open}
				fileName={fileName}
				reopenKey={opens.count}
				events={events}
				className="min-h-0 flex-1"
				notices={
					<>
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
					</>
				}
			/>
		</aside>
	);
}
