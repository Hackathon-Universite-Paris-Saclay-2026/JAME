/**
 * Pure search/replace engine — computes range-based edit descriptors without
 * touching the workspace.  All mutation happens in a single WorkspaceEdit
 * applied by the caller (extension.ts or a command handler).
 *
 * Features:
 *  - Literal or regex pattern matching
 *  - Case-sensitivity toggle
 *  - Multi-line dot-matches-newline support
 *  - Overlap-safe: earlier matches are not re-matched after a prior replacement
 *  - Returns EditDescriptor[] — one per non-overlapping match found
 */

import * as vscode from "vscode";

export interface SearchReplaceOptions {
  /** The search string or regex source. */
  pattern: string;
  /** Replacement string.  Regex back-references ($1, $2, …) supported in regex mode. */
  replacement: string;
  /** When true, interpret pattern as a regex; otherwise literal. Default: false. */
  isRegex?: boolean;
  /** When true, match is case-sensitive.  Default: true. */
  caseSensitive?: boolean;
  /** When true, '.' in regex matches newlines.  Default: false. */
  multiline?: boolean;
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

/**
 * Build a RegExp from the given options, escaping literal patterns as needed.
 * Throws if the user-supplied regex pattern is syntactically invalid.
 */
export function buildRegExp(opts: SearchReplaceOptions): RegExp {
  const source = opts.isRegex ? opts.pattern : escapeRegExp(opts.pattern);
  const flags = [
    "g",
    opts.caseSensitive === false ? "i" : "",
    opts.multiline ? "s" : "",
  ]
    .filter(Boolean)
    .join("");
  return new RegExp(source, flags);
}

/** Escape all RegExp metacharacters in a literal string. */
function escapeRegExp(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/**
 * Compute all non-overlapping edit descriptors for *one* document.
 *
 * The function opens the document (or reuses the in-memory version) and
 * scans its text with the compiled regex.  It never writes anything.
 */
export async function computeEditsForDocument(
  uri: vscode.Uri,
  regex: RegExp,
  replacement: string
): Promise<EditDescriptor[]> {
  const doc = await vscode.workspace.openTextDocument(uri);
  const text = doc.getText();

  // Reset lastIndex so the same RegExp object can be reused across documents
  regex.lastIndex = 0;

  const edits: EditDescriptor[] = [];
  let match: RegExpExecArray | null;

  while ((match = regex.exec(text)) !== null) {
    const start = doc.positionAt(match.index);
    const end = doc.positionAt(match.index + match[0].length);
    const replacementText = match[0].replace(regex, replacement);

    edits.push({
      uri,
      range: new vscode.Range(start, end),
      matchedText: match[0],
      replacementText,
    });

    // Prevent infinite loops on zero-width matches
    if (match[0].length === 0) {
      regex.lastIndex++;
    }
  }

  return edits;
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
  confirmThreshold = 200
): Promise<number> {
  let regex: RegExp;
  try {
    regex = buildRegExp(opts);
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
    const docEdits = await computeEditsForDocument(uri, regex, opts.replacement);
    allEdits.push(...docEdits);
  }

  if (allEdits.length === 0) {
    vscode.window.showInformationMessage(
      `No matches found for "${opts.pattern}".`
    );
    return 0;
  }

  // Confirmation gate for large change sets
  if (allEdits.length > confirmThreshold) {
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
