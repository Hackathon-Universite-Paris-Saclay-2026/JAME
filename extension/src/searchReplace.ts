import * as vscode from "vscode";
import {
  buildRegExp as buildCoreRegExp,
  computeTextEdits,
  type SearchReplaceOptions,
} from "./searchReplaceCore";

export type { SearchReplaceOptions } from "./searchReplaceCore";

export interface ResolvedFileForSearch {
  relativePath: string;
  content: string;
  source: "proposed" | "workspace" | "generated" | "not_found";
  uri?: vscode.Uri;
}

export interface EditDescriptor {
  /** Absolute URI of the file that contains this match. */
  uri: vscode.Uri;
  /** VSCode range of the match (start and end positions in the document). */
  range: vscode.Range;
  /** The matched text that will be replaced. */
  matchedText: string;
  /** The replacement text after substitution. */
  replacementText: string;
}

export function buildRegExp(opts: SearchReplaceOptions): RegExp {
  return buildCoreRegExp(opts);
}

/**
 * Compute all non-overlapping edit descriptors for *one* document.
 *
 * The function opens the document (or reuses the in-memory version) and
 * scans its text with the compiled regex.  It never writes anything.
 */
export async function computeEditsForDocument(
  uri: vscode.Uri,
  opts: SearchReplaceOptions
): Promise<EditDescriptor[]> {
  const doc = await vscode.workspace.openTextDocument(uri);
  return computeEditsForResolvedFile(
    {
      relativePath: uri.fsPath,
      content: doc.getText(),
      source: "workspace",
      uri,
    },
    {
      pattern: opts.pattern,
      replacement: opts.replacement,
      isRegex: opts.isRegex,
      caseSensitive: opts.caseSensitive,
      multiline: opts.multiline,
    }
  );
}

export function computeEditsForResolvedFile(
  file: ResolvedFileForSearch,
  opts: SearchReplaceOptions
): EditDescriptor[] {
  if (!file.uri || file.source === "not_found") {
    return [];
  }

  const spans = computeTextEdits(file.content, opts);
  if (spans.length === 0) {
    return [];
  }

  const docLike = createLineIndex(file.content);
  return spans.map((span) => ({
    uri: file.uri!,
    range: new vscode.Range(
      docLike.positionAt(span.startOffset),
      docLike.positionAt(span.endOffset)
    ),
    matchedText: span.matchedText,
    replacementText: span.replacementText,
  }));
}

function createLineIndex(text: string): { positionAt: (offset: number) => vscode.Position } {
  const lineStarts = [0];
  for (let i = 0; i < text.length; i++) {
    if (text.charCodeAt(i) === 10) {
      lineStarts.push(i + 1);
    }
  }

  return {
    positionAt(offset: number): vscode.Position {
      const clamped = Math.max(0, Math.min(offset, text.length));
      let low = 0;
      let high = lineStarts.length - 1;
      while (low <= high) {
        const mid = (low + high) >> 1;
        if (lineStarts[mid] <= clamped) {
          low = mid + 1;
        } else {
          high = mid - 1;
        }
      }
      const line = Math.max(0, high);
      return new vscode.Position(line, clamped - lineStarts[line]);
    },
  };
}

/**
 * Apply an array of EditDescriptors as a single atomic WorkspaceEdit.
 *
 * The edits are applied in *reverse document order* so that earlier ranges
 * are not invalidated by later replacements in the same file.
 */
export function applyEdits(edits: EditDescriptor[]): Thenable<boolean> {
  const wsEdit = new vscode.WorkspaceEdit();

  // Group by URI and sort each group in reverse range order
  const byUri = new Map<string, EditDescriptor[]>();
  for (const ed of edits) {
    const key = ed.uri.toString();
    if (!byUri.has(key)) byUri.set(key, []);
    byUri.get(key)!.push(ed);
  }

  for (const group of byUri.values()) {
    // Reverse sort: later ranges first so offsets stay valid
    group.sort((a, b) => (b.range.start.isBefore(a.range.start) ? -1 : 1));
    for (const ed of group) {
      wsEdit.replace(ed.uri, ed.range, ed.replacementText);
    }
  }

  return vscode.workspace.applyEdit(wsEdit);
}

/**
 * High-level helper: search across a set of URIs, gather all matches,
 * optionally confirm if count > threshold, then apply.
 *
 * Returns the total number of replacements applied, or -1 if cancelled.
 */
export async function searchReplaceAcrossFiles(
  uris: vscode.Uri[],
  opts: SearchReplaceOptions,
  confirmThreshold = 1
): Promise<number> {
  try {
    buildRegExp(opts);
  } catch (err) {
    vscode.window.showErrorMessage(
      `Invalid search pattern: ${err instanceof Error ? err.message : String(err)}`
    );
    return -1;
  }

  if (!opts.pattern) {
    vscode.window.showWarningMessage("Search pattern is empty — nothing to do.");
    return 0;
  }

  // Gather all edit descriptors
  const allEdits: EditDescriptor[] = [];
  for (const uri of uris) {
    const docEdits2 = await computeEditsForDocument(uri, opts);
    allEdits.push(...docEdits2);
  }

  if (allEdits.length === 0) {
    vscode.window.showInformationMessage(
      `No matches found for "${opts.pattern}".`
    );
    return 0;
  }

  // Confirmation gate for large change sets
  if (allEdits.length >= confirmThreshold) {
    const choice = await vscode.window.showWarningMessage(
      `Search/replace will modify ${allEdits.length} locations across ${uris.length} file(s).  Apply?`,
      { modal: true },
      "Apply",
      "Cancel"
    );
    if (choice !== "Apply") {
      return -1;
    }
  }

  const ok = await applyEdits(allEdits);
  if (!ok) {
    vscode.window.showErrorMessage("WorkspaceEdit could not be applied.");
    return -1;
  }

  vscode.window.showInformationMessage(
    `Applied ${allEdits.length} replacement(s) across ${new Set(allEdits.map((e) => e.uri.toString())).size} file(s).`
  );
  return allEdits.length;
}

export async function searchReplaceAcrossResolvedFiles(
  files: ResolvedFileForSearch[],
  opts: SearchReplaceOptions,
  confirmThreshold = 1
): Promise<number> {
  try {
    buildRegExp(opts);
  } catch (err) {
    vscode.window.showErrorMessage(
      `Invalid search pattern: ${err instanceof Error ? err.message : String(err)}`
    );
    return -1;
  }

  if (!opts.pattern) {
    vscode.window.showWarningMessage("Search pattern is empty — nothing to do.");
    return 0;
  }

  const allEdits: EditDescriptor[] = [];
  for (const file of files) {
    if (!file.uri || file.source === "not_found") {
      continue;
    }
    const edits = computeEditsForResolvedFile(file, opts);
    allEdits.push(...edits);
  }

  if (allEdits.length === 0) {
    vscode.window.showInformationMessage(`No matches found for "${opts.pattern}".`);
    return 0;
  }

  if (allEdits.length >= confirmThreshold) {
    const choice = await vscode.window.showWarningMessage(
      `Search/replace will modify ${allEdits.length} locations across ${new Set(allEdits.map((e) => e.uri.toString())).size} file(s). Apply?`,
      { modal: true },
      "Apply",
      "Cancel"
    );
    if (choice !== "Apply") {
      return -1;
    }
  }

  const ok = await applyEdits(allEdits);
  if (!ok) {
    vscode.window.showErrorMessage("WorkspaceEdit could not be applied.");
    return -1;
  }

  vscode.window.showInformationMessage(
    `Applied ${allEdits.length} replacement(s) across ${new Set(allEdits.map((e) => e.uri.toString())).size} file(s).`
  );
  return allEdits.length;
}
