/**
 * File tree state management with REST fetch and live updates.
 * Supports workspace navigation, file preview, upload, and mkdir.
 */

import { useState, useEffect, useCallback } from "react";
import type { FileEntry, FilePreview } from "../types";

interface FileTreeState {
  entries: FileEntry[];
  loading: boolean;
  error: string | null;
}

export function useFileTree(fileChanges: { event: string; path: string }[]) {
  const [tree, setTree] = useState<FileTreeState>({
    entries: [],
    loading: true,
    error: null,
  });
  const [selectedFile, setSelectedFile] = useState<FilePreview | null>(null);
  const [expandedDirs, setExpandedDirs] = useState<Set<string>>(new Set(["/"]));
  const [workspacePath, setWorkspacePath] = useState("/");

  /**
   * Append session_id (Arlie playground UUID from ?sessionId= page param) to
   * any file API URL.  index.html does the same injection at the fetch intercept
   * level, but that only fires when the param is absent — this ensures it is
   * present from the start so the middleware can scope the workspace correctly.
   */
  const playgroundSid = new URLSearchParams(window.location.search).get("sessionId") ?? "";

  const withSid = useCallback(
    (url: string) => {
      if (!playgroundSid) return url;
      const sep = url.includes("?") ? "&" : "?";
      return `${url}${sep}session_id=${encodeURIComponent(playgroundSid)}`;
    },
    // playgroundSid is derived from window.location.search which never changes
    // during a session — the dep array can be empty to avoid recreating the callback.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );

  const fetchTree = useCallback(async (path = "/", depth = 1) => {
    try {
      const res = await fetch(
        withSid(`/api/files/tree?path=${encodeURIComponent(path)}&depth=${depth}`)
      );
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      return data.entries as FileEntry[];
    } catch (err) {
      console.error("Failed to fetch file tree:", err);
      return [];
    }
  }, [withSid]);

  const loadRoot = useCallback(async () => {
    setTree((s) => ({ ...s, loading: true }));
    const entries = await fetchTree(workspacePath, 1);
    setTree({ entries, loading: false, error: null });
  }, [fetchTree, workspacePath]);

  useEffect(() => {
    loadRoot();
  }, [loadRoot]);

  // Refresh on file changes
  useEffect(() => {
    if (fileChanges.length > 0) {
      loadRoot();
    }
  }, [fileChanges, loadRoot]);

  const toggleDir = useCallback(
    async (path: string) => {
      setExpandedDirs((prev) => {
        const next = new Set(prev);
        if (next.has(path)) {
          next.delete(path);
        } else {
          next.add(path);
        }
        return next;
      });

      // Fetch children if not loaded
      setTree((prev) => {
        const updateChildren = (entries: FileEntry[]): FileEntry[] =>
          entries.map((e) => {
            if (e.path === path && e.is_dir && !e.children) {
              return { ...e, children: [] };
            }
            if (e.children) {
              return { ...e, children: updateChildren(e.children) };
            }
            return e;
          });
        return { ...prev, entries: updateChildren(prev.entries) };
      });

      const children = await fetchTree(path, 1);
      setTree((prev) => {
        const setChildren = (entries: FileEntry[]): FileEntry[] =>
          entries.map((e) => {
            if (e.path === path) {
              return { ...e, children };
            }
            if (e.children) {
              return { ...e, children: setChildren(e.children) };
            }
            return e;
          });
        return { ...prev, entries: setChildren(prev.entries) };
      });
    },
    [fetchTree]
  );

  const openFile = useCallback(async (path: string) => {
    try {
      const res = await fetch(
        withSid(`/api/files/preview?path=${encodeURIComponent(path)}`)
      );
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data: FilePreview = await res.json();
      setSelectedFile(data);
    } catch (err) {
      console.error("Failed to preview file:", err);
    }
  }, [withSid]);

  const enterDir = useCallback(
    (path: string) => {
      setWorkspacePath(path);
      setExpandedDirs(new Set([path]));
      setSelectedFile(null);
    },
    []
  );

  const uploadFile = useCallback(
    async (file: File): Promise<{ success: boolean; error?: string }> => {
      const form = new FormData();
      form.append("file", file);
      try {
        const res = await fetch(
          withSid(`/api/files/upload?path=${encodeURIComponent(workspacePath)}`),
          { method: "POST", body: form }
        );
        if (!res.ok) {
          let detail = `HTTP ${res.status}`;
          try {
            const body = await res.json();
            if (body?.detail) detail = String(body.detail);
          } catch {
            // non-JSON body — keep the status code string
          }
          return { success: false, error: detail };
        }
        loadRoot();
        return { success: true };
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        console.error("Failed to upload file:", err);
        return { success: false, error: msg };
      }
    },
    [workspacePath, loadRoot, withSid]
  );

  const createFolder = useCallback(
    async (name: string) => {
      const folderPath =
        workspacePath === "/"
          ? `/${name}`
          : `${workspacePath}/${name}`;
      try {
        const res = await fetch(withSid("/api/files/mkdir"), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: folderPath }),
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        loadRoot();
      } catch (err) {
        console.error("Failed to create folder:", err);
      }
    },
    [workspacePath, loadRoot, withSid]
  );

  const deletePath = useCallback(
    async (path: string) => {
      try {
        const res = await fetch(withSid("/api/files/delete"), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path }),
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        loadRoot();
      } catch (err) {
        console.error("Failed to delete:", err);
      }
    },
    [loadRoot, withSid]
  );

  return {
    tree,
    selectedFile,
    expandedDirs,
    workspacePath,
    toggleDir,
    openFile,
    enterDir,
    uploadFile,
    createFolder,
    deletePath,
    setSelectedFile,
    refresh: loadRoot,
  };
}
