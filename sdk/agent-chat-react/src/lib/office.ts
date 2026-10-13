const OFFICE_EXTENSIONS = new Set(["docx", "xlsx", "pptx", "pdf", "doc", "odt", "rtf", "xls", "ods", "ppt", "odp"]);

/** Whether the document server opens this file. */
export function isOfficeDocument(path: string): boolean {
  const dot = path.lastIndexOf(".");
  return dot >= 0 && OFFICE_EXTENSIONS.has(path.slice(dot + 1).toLowerCase());
}

export interface DocEditorInstance {
  destroyEditor(): void;
}

declare global {
  interface Window {
    DocsAPI?: {
      DocEditor: new (placeholderId: string, config: Record<string, unknown>) => DocEditorInstance;
    };
  }
}

const loads = new Map<string, Promise<void>>();
const LOAD_TIMEOUT_MS = 30_000;

/** Loads the document server's `api.js` once per server. */
export function loadDocsApi(documentServerUrl: string): Promise<void> {
  const src = `${documentServerUrl.replace(/\/$/, "")}/web-apps/apps/api/documents/api.js`;
  let load = loads.get(src);
  if (!load) {
    load = new Promise<void>((resolve, reject) => {
      const script = document.createElement("script");
      // A failed or hung load leaves no cache entry and no script, so the next open retries.
      const settle = (loaded: boolean) => {
        clearTimeout(timer);
        script.onload = script.onerror = null;
        if (loaded) return resolve();
        loads.delete(src);
        script.remove();
        reject(new Error("The document editor could not be loaded."));
      };
      const timer = setTimeout(() => settle(false), LOAD_TIMEOUT_MS);
      script.src = src;
      script.async = true;
      script.onload = () => settle(Boolean(window.DocsAPI));
      script.onerror = () => settle(false);
      document.head.appendChild(script);
    });
    loads.set(src, load);
  }
  return load;
}
