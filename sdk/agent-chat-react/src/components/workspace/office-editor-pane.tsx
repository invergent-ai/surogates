import { useEffect, useId, useState } from "react";
import { useTheme } from "next-themes";
import { AlertCircleIcon, DownloadIcon, FileTextIcon, XIcon } from "lucide-react";
import { type DocEditorInstance, loadDocsApi } from "../../lib/office";
import type { AgentChatAdapter } from "../../types";

interface OfficeEditorPaneProps {
	adapter: AgentChatAdapter;
	sessionId: string;
	path: string;
	view: boolean;
	onClose: () => void;
}

/** An office document open in the document server's editor, in the file pane. */
export function OfficeEditorPane({ adapter, sessionId, path, view, onClose }: OfficeEditorPaneProps) {
	const { resolvedTheme } = useTheme();
	const theme = resolvedTheme === "dark" ? "dark" : "light";
	const placeholderId = `office-editor-${useId().replace(/[^a-zA-Z0-9_-]/g, "")}`;
	const [error, setError] = useState<string | null>(null);
	const [loading, setLoading] = useState(true);

	useEffect(() => {
		let cancelled = false;
		let editor: DocEditorInstance | null = null;
		setError(null);
		setLoading(true);
		void (async () => {
			try {
				if (!adapter.getWorkspaceEditorConfig) throw new Error("This app cannot open office documents.");
				const opened = await adapter.getWorkspaceEditorConfig({ sessionId, path, theme, view });
				await loadDocsApi(opened.document_server_url);
				if (cancelled) return;
				if (!window.DocsAPI) throw new Error("The document editor could not be loaded.");
				editor = new window.DocsAPI.DocEditor(placeholderId, opened.config);
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
	}, [adapter, sessionId, path, theme, view, placeholderId]);

	const fileName = path.split("/").pop() ?? path;
	return (
		<aside
			data-testid="file-preview-panel"
			className="flex h-full min-h-0 w-full flex-col overflow-hidden border-l border-line bg-card"
		>
			<div className="flex shrink-0 items-center gap-2 border-b border-line px-3 py-2">
				<FileTextIcon className="size-4 shrink-0 text-muted-foreground" />
				<div className="min-w-0 flex-1 truncate font-medium">{fileName}</div>
				<a
					href={adapter.getWorkspaceDownloadUrl({ sessionId, path })}
					download={fileName}
					aria-label={`Download ${fileName}`}
					title="Download"
					className="shrink-0 rounded p-0.5 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
				>
					<DownloadIcon className="size-4" />
				</a>
				<button
					type="button"
					onClick={onClose}
					aria-label="Close file"
					className="shrink-0 rounded p-0.5 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
				>
					<XIcon className="size-4" />
				</button>
			</div>
			{error && (
				<div role="alert" className="flex items-start gap-2 p-3 text-xs text-destructive">
					<AlertCircleIcon className="mt-0.5 size-3.5 shrink-0" />
					<span>{error}</span>
				</div>
			)}
			{loading && !error && <div className="p-3 text-sm text-muted-foreground">Opening {fileName}…</div>}
			<div className="min-h-0 flex-1">
				<div id={placeholderId} />
			</div>
		</aside>
	);
}
