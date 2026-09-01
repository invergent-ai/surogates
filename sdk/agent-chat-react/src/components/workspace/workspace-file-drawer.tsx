import { useEffect, useRef, useState } from "react";
import type {
	AgentChatAdapter,
	AgentChatWorkspaceFile,
} from "../../types";
import { FileViewer } from "./file-viewer";

/**
 * The file preview as a right-column drawer.
 *
 * Same slot and geometry as the browser pane: a file is a document you read,
 * not a strip you squint at inside the accordion. The tree stays in the
 * accordion; this only ever shows one file, and closing it returns the column.
 * Deletion is deliberately absent here — the tree rows carry it, with the
 * confirm dialog, and one home for a destructive action is enough.
 */

interface WorkspaceFileDrawerProps {
	adapter: AgentChatAdapter;
	sessionId: string;
	path: string;
	onClose: () => void;
}

export function WorkspaceFileDrawer({
	adapter,
	sessionId,
	path,
	onClose,
}: WorkspaceFileDrawerProps) {
	const [file, setFile] = useState<AgentChatWorkspaceFile | null>(null);
	const [loading, setLoading] = useState(true);
	const [error, setError] = useState<string | null>(null);
	// A slow fetch resolving after the user picked another file must not
	// paint the stale document over the new one.
	const requestRef = useRef(0);

	useEffect(() => {
		const request = ++requestRef.current;
		// A new target clears the old document at once: the previous file
		// sitting under the next one's loading skeleton reads as the wrong
		// file having loaded.
		setFile(null);
		setLoading(true);
		setError(null);
		void (async () => {
			try {
				const next = await adapter.getWorkspaceFile({ sessionId, path });
				if (requestRef.current !== request) return;
				setFile(next);
			} catch (fetchError) {
				if (requestRef.current !== request) return;
				setFile(null);
				setError((fetchError as Error).message);
			} finally {
				if (requestRef.current === request) setLoading(false);
			}
		})();
	}, [adapter, sessionId, path]);

	return (
		<aside
			data-testid="file-preview-panel"
			className="flex h-full min-h-0 w-full flex-col overflow-hidden border-l border-line bg-card"
		>
			<FileViewer
				className="border-t-0"
				file={file}
				loading={loading}
				error={error}
				downloadUrl={
					file
						? adapter.getWorkspaceDownloadUrl({ sessionId, path: file.path })
						: null
				}
				onDelete={null}
				onClose={onClose}
			/>
		</aside>
	);
}
