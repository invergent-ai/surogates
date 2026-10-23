import { type ReactNode, useEffect, useId, useRef, useState } from "react";
import { useTheme } from "next-themes";
import { AlertCircleIcon } from "lucide-react";
import { type DocEditorEvents, type DocEditorInstance, loadDocsApi } from "../../lib/office";
import { cn } from "../../lib/utils";
import type { AgentChatMentionableUser } from "../../types";

/** A document opened for the document server's editor. */
export interface EditorOpening {
	/** Where the browser loads the editor (`<url>/web-apps/apps/api/documents/api.js`). */
	document_server_url: string;
	/** The signed editor config, passed to `DocsAPI.DocEditor` as signed, with only the `events` built for it beside it. */
	config: Record<string, unknown>;
	/** Who a comment can mention. */
	mentionable_users?: AgentChatMentionableUser[];
}

export interface DocumentEditorProps {
	/** Opens the document for the editor, in the page's theme. */
	open: (theme: "light" | "dark") => Promise<EditorOpening>;
	/** The document's name, shown while it opens. */
	fileName: string;
	/** A new value closes the editor and opens the document again. A new `open` alone does not. */
	reopenKey?: string | number;
	/**
	 * The editor's event handlers for one opening, or null for none. *editor* answers the open editor,
	 * and null before it is made and once it is closed.
	 */
	events?: (editor: () => DocEditorInstance | null, opening: EditorOpening) => Partial<DocEditorEvents> | null;
	/** The host's own lines, such as its save notices: shown under the error alert, above the editor. */
	notices?: ReactNode;
	className?: string;
}

/** What the editor shows for a failure that carries no message of its own. */
const COULD_NOT_OPEN = "The document could not be opened.";

function messageOf(failure: unknown): string {
	return failure instanceof Error && failure.message ? failure.message : COULD_NOT_OPEN;
}

/** A document in the document server's editor, opened by the host's `open`, with no chat session behind it. */
export function DocumentEditor({ open, fileName, reopenKey, events, notices, className }: DocumentEditorProps) {
	const { resolvedTheme } = useTheme();
	const theme = resolvedTheme === "dark" ? "dark" : "light";
	const placeholderId = `office-editor-${useId().replace(/[^a-zA-Z0-9_-]/g, "")}`;
	const [error, setError] = useState<string | null>(null);
	const [loading, setLoading] = useState(true);
	// The latest `open` and `events` serve the next opening; only the theme and `reopenKey` start one.
	const openRef = useRef(open);
	openRef.current = open;
	const eventsRef = useRef(events);
	eventsRef.current = events;

	useEffect(() => {
		// An opening the next one replaced, or the unmount ended, never makes its editor.
		let live = true;
		let editor: DocEditorInstance | null = null;
		setError(null);
		setLoading(true);
		void (async () => {
			try {
				const opening = await openRef.current(theme);
				if (!live) return;
				await loadDocsApi(opening.document_server_url);
				if (!live) return;
				if (!window.DocsAPI) throw new Error("The document editor could not be loaded.");
				const handlers = eventsRef.current?.(() => editor, opening) ?? null;
				editor = new window.DocsAPI.DocEditor(placeholderId, handlers ? { ...opening.config, events: handlers } : opening.config);
			} catch (openError) {
				if (live) setError(messageOf(openError));
			} finally {
				if (live) setLoading(false);
			}
		})();
		return () => {
			live = false;
			editor?.destroyEditor();
			editor = null;
		};
	}, [theme, reopenKey, placeholderId]);

	return (
		<div className={cn("flex min-h-0 flex-col", className)}>
			{error && (
				<div role="alert" className="flex items-start gap-2 p-3 text-xs text-destructive">
					<AlertCircleIcon className="mt-0.5 size-3.5 shrink-0" />
					<span>{error}</span>
				</div>
			)}
			{notices}
			{loading && !error && <div className="p-3 text-sm text-muted-foreground">Opening {fileName}…</div>}
			<div className="min-h-0 flex-1">
				<div id={placeholderId} />
			</div>
		</div>
	);
}
