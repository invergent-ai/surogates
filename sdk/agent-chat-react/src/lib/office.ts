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

/** Loads the document server's `api.js` once per server. */
export function loadDocsApi(documentServerUrl: string): Promise<void> {
  const src = `${documentServerUrl.replace(/\/$/, "")}/web-apps/apps/api/documents/api.js`;
  let load = loads.get(src);
  if (!load) {
    load = new Promise<void>((resolve, reject) => {
      const script = document.createElement("script");
      script.src = src;
      script.async = true;
      script.onload = () => resolve();
      script.onerror = () => {
        loads.delete(src);
        script.remove();
        reject(new Error("The document editor could not be loaded."));
      };
      document.head.appendChild(script);
    });
    loads.set(src, load);
  }
  return load;
}
