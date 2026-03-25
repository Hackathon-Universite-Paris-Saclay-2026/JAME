/**
 * Unified file-reading abstraction.
 *
 * Resolves content from three possible sources, in precedence order:
 *   1. In-memory proposed content (from JameViewProvider.proposedFiles)
 *   2. Workspace file on disk
 *   3. Generated/proposed artifact from a JAME run (fetched from backend API)
 *
 * When sources 1 and 3 both exist for the same relative path, source 1 takes
 * precedence (it is the most recent version the user has seen in the diff
 * editor).  A conflict warning is emitted when both exist with different
 * content so the caller can surface it to the user.
 */

import * as fs from "node:fs";
import * as path from "node:path";
import * as vscode from "vscode";

export interface FileReadResult {
  /** Relative path as provided by the caller. */
  relativePath: string;
  /** Resolved content string. */
  content: string;
  /** Indicates where the content came from. */
  source: "proposed" | "workspace" | "generated" | "not_found";
  /** Warning message when a conflict was detected between sources. */
  conflictWarning?: string;
}

export interface FileReaderOptions {
  /**
   * Map of relative path → proposed content held by JameViewProvider.
   * Passed in by reference so it reflects live state.
   */
  proposedFiles: Map<string, string>;
  /** Absolute path to the workspace root (first workspace folder). */
  workspaceRoot?: string;
  /**
   * Map of relative path → generated content from the most recent run.
   * Populated from file_generated events during a run.
   */
  generatedFiles?: Map<string, string>;
}

/**
 * Read the content of *relativePath* using the precedence rules documented
 * above.  Never throws — returns a FileReadResult with source="not_found"
 * on total failure.
 */
export async function readFile(
  relativePath: string,
  opts: FileReaderOptions
): Promise<FileReadResult> {
  const { proposedFiles, workspaceRoot, generatedFiles } = opts;

  // ── Source 1: In-memory proposed content ──────────────────────────────
  const proposed = proposedFiles.get(relativePath);

  // ── Source 2: Workspace file ───────────────────────────────────────────
  let workspaceContent: string | undefined;
  if (workspaceRoot) {
    const absPath = path.join(workspaceRoot, relativePath);
    if (fs.existsSync(absPath)) {
      try {
        workspaceContent = fs.readFileSync(absPath, "utf8");
      } catch {
        // ignore read errors — treat as not found
      }
    }
  }

  // ── Source 3: Generated/proposed artifact ─────────────────────────────
  const generated = generatedFiles?.get(relativePath);

  // ── Precedence + conflict detection ───────────────────────────────────
  if (proposed !== undefined) {
    let conflictWarning: string | undefined;
    if (generated !== undefined && generated !== proposed) {
      conflictWarning =
        `"${relativePath}" has both proposed (diff-editor) and generated content — ` +
        "using proposed (diff-editor) version.";
    }
    return {
      relativePath,
      content: proposed,
      source: "proposed",
      conflictWarning,
    };
  }

  if (workspaceContent !== undefined) {
    return { relativePath, content: workspaceContent, source: "workspace" };
  }

  if (generated !== undefined) {
    return { relativePath, content: generated, source: "generated" };
  }

  return { relativePath, content: "", source: "not_found" };
}

/**
 * Read multiple files in parallel.  Results preserve input order.
 * Files not found in any source are included with source="not_found".
 */
export async function readFiles(
  relativePaths: string[],
  opts: FileReaderOptions
): Promise<FileReadResult[]> {
  return Promise.all(relativePaths.map((p) => readFile(p, opts)));
}

/**
 * Collect all URIs for files that exist in the workspace root,
 * optionally filtered by a glob pattern.  Returns an empty array when
 * no workspace folder is open.
 */
export async function listWorkspaceUris(
  globPattern = "**/*",
  excludePattern = "**/node_modules/**"
): Promise<vscode.Uri[]> {
  return vscode.workspace.findFiles(globPattern, excludePattern);
}
