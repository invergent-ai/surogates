// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import type { StateCreator } from "zustand";
import type { AppState } from "./app-store";
import * as workspaceApi from "@/api/workspace";
import type { FileEntry, FileContentResponse } from "@/api/workspace";

export type WorkspaceSlice = {
  workspaceTree: FileEntry[];
  workspaceRoot: string | null;
  workspaceTreeLoading: boolean;
  workspaceTreeError: string | null;
  workspaceTruncated: boolean;

  workspaceFile: FileContentResponse | null;
  workspaceFileLoading: boolean;
  workspaceFileError: string | null;

  workspacePanelOpen: boolean;

  /** Currently selected path in the file tree. */
  selectedFilePath: string | undefined;

  /** Maps toolCallId -> checkpoint hash for file-mutating tool calls. */
  toolCheckpoints: Record<string, string>;

  fetchWorkspaceTree: (sessionId: string) => Promise<void>;
  fetchWorkspaceFile: (sessionId: string, path: string) => Promise<void>;
  clearWorkspaceFile: () => void;
  clearWorkspace: () => void;
  setWorkspacePanelOpen: (open: boolean) => void;
  setSelectedFilePath: (path: string | undefined) => void;
  setToolCheckpoint: (toolCallId: string, hash: string) => void;
};

export const createWorkspaceSlice: StateCreator<
  AppState,
  [],
  [],
  WorkspaceSlice
> = (set, get) => ({
  workspaceTree: [],
  workspaceRoot: null,
  workspaceTreeLoading: false,
  workspaceTreeError: null,
  workspaceTruncated: false,

  workspaceFile: null,
  workspaceFileLoading: false,
  workspaceFileError: null,

  workspacePanelOpen: false,

  selectedFilePath: undefined,

  toolCheckpoints: {},

  fetchWorkspaceTree: async (sessionId) => {
    set({
      workspaceTreeLoading: true,
      workspaceTreeError: null,
    });
    try {
      const res = await workspaceApi.getWorkspaceTree(sessionId);
      // Guard against stale responses when the user switched sessions.
      if (get().activeSessionId !== sessionId) return;
      set({
        workspaceTree: res.entries,
        workspaceRoot: res.root,
        workspaceTruncated: res.truncated,
        workspaceTreeLoading: false,
      });
    } catch (e) {
      if (get().activeSessionId !== sessionId) return;
      set({
        workspaceTree: [],
        workspaceRoot: null,
        workspaceTreeLoading: false,
        workspaceTreeError: (e as Error).message,
      });
    }
  },

  fetchWorkspaceFile: async (sessionId, path) => {
    set({ workspaceFileLoading: true, workspaceFileError: null });
    try {
      const res = await workspaceApi.getWorkspaceFile(sessionId, path);
      if (get().activeSessionId !== sessionId) return;
      // Use the resolved relative path from the API response for
      // file tree selection (the input path may be absolute).
      set({
        workspaceFile: res,
        workspaceFileLoading: false,
      });
    } catch (e) {
      if (get().activeSessionId !== sessionId) return;
      set({
        workspaceFile: null,
        workspaceFileLoading: false,
        workspaceFileError: (e as Error).message,
      });
    }
  },

  clearWorkspaceFile: () => set({ workspaceFile: null, workspaceFileError: null }),

  clearWorkspace: () =>
    set({
      workspaceTree: [],
      workspaceRoot: null,
      workspaceTreeLoading: false,
      workspaceTreeError: null,
      workspaceTruncated: false,
      workspaceFile: null,
      workspaceFileLoading: false,
      workspaceFileError: null,
      toolCheckpoints: {},
    }),

  setWorkspacePanelOpen: (open) => set({ workspacePanelOpen: open }),

  setSelectedFilePath: (path) => set({ selectedFilePath: path }),

  setToolCheckpoint: (toolCallId, hash) =>
    set((s) => ({
      toolCheckpoints: { ...s.toolCheckpoints, [toolCallId]: hash },
    })),
});
