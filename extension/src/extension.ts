import * as fs from "node:fs";
import * as path from "node:path";
import * as vscode from "vscode";
import { JameViewProvider } from "./view";
import {
  searchReplaceAcrossResolvedFiles,
  buildRegExp,
  type ResolvedFileForSearch,
} from "./searchReplace";
import { listWorkspaceUris, readFiles } from "./fileReader";

export function activate(context: vscode.ExtensionContext): void {
  const provider = new JameViewProvider(context.extensionUri);

  context.subscriptions.push(
    vscode.window.registerWebviewViewProvider("jameWorkflow.agent", provider, {
      webviewOptions: { retainContextWhenHidden: true },
    })
  );

  // Register jame-proposed:// virtual document provider for inline diffs
  const contentProvider: vscode.TextDocumentContentProvider = {
    provideTextDocumentContent(uri: vscode.Uri): string {
      if (uri.path === "/__empty__") return "";
      return provider.getProposedContent(uri.path) ?? "";
    },
  };
  context.subscriptions.push(
    vscode.workspace.registerTextDocumentContentProvider("jame-proposed", contentProvider)
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("jameWorkflow.focus", () => {
      provider.focus();
    })
  );

  // Helper: close all diff tabs for a given file path
  async function closeDiffTabsForPath(filePath: string): Promise<void> {
    for (const group of vscode.window.tabGroups.all) {
      for (const tab of group.tabs) {
        const input = tab.input as { original?: vscode.Uri; modified?: vscode.Uri } | undefined;
        // Match tabs where either side references this file (jame-proposed or real file)
        const modPath = input?.modified?.path?.replace(/^\//, "") ?? "";
        const origPath = input?.original?.path?.replace(/^\//, "") ?? "";
        if (modPath === filePath || origPath === filePath ||
            modPath.endsWith("/" + filePath) || origPath.endsWith("/" + filePath)) {
          await vscode.window.tabGroups.close(tab);
        }
      }
    }
  }

  // Accept a single proposed file — file is already on disk, just close the diff tab
  context.subscriptions.push(
    vscode.commands.registerCommand("jameWorkflow.acceptFile", async (uri?: vscode.Uri) => {
      // Called from panel ✓ button: uri is the file path string passed via postMessage
      // Called from diff editor title: uri is the jame-proposed URI
      let filePath: string | undefined;
      if (uri instanceof vscode.Uri) {
        filePath = uri.path.replace(/^\//, "");
      }
      if (!filePath) {
        vscode.window.showWarningMessage("No JAME proposed file is active.");
        return;
      }
      // File is already written — just close the diff tab
      await closeDiffTabsForPath(filePath);
      vscode.window.showInformationMessage(`Kept: ${path.basename(filePath)}`);
    })
  );

  // Discard a single proposed file — delete it from disk and close the diff tab
  context.subscriptions.push(
    vscode.commands.registerCommand("jameWorkflow.discardFile", async (uri?: vscode.Uri) => {
      let filePath: string | undefined;
      if (uri instanceof vscode.Uri) {
        filePath = uri.path.replace(/^\//, "");
      }
      if (!filePath) {
        vscode.window.showWarningMessage("No JAME proposed file is active.");
        return;
      }
      const workspaceRoot = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
      if (workspaceRoot) {
        const dest = path.join(workspaceRoot, filePath);
        if (fs.existsSync(dest)) {
          fs.unlinkSync(dest);
        }
      }
      await closeDiffTabsForPath(filePath);
      vscode.window.showInformationMessage(`Discarded: ${path.basename(filePath)}`);
    })
  );

  // Accept all — files already on disk, just close all JAME diff tabs
  context.subscriptions.push(
    vscode.commands.registerCommand("jameWorkflow.acceptAll", async () => {
      for (const group of vscode.window.tabGroups.all) {
        for (const tab of group.tabs) {
          const input = tab.input as { original?: vscode.Uri; modified?: vscode.Uri } | undefined;
          if (input?.original?.scheme === "jame-proposed") {
            await vscode.window.tabGroups.close(tab);
          }
        }
      }
    })
  );

  // Smart search/replace across workspace files
  context.subscriptions.push(
    vscode.commands.registerCommand("jameWorkflow.searchReplaceSmart", async () => {
      // Step 1: gather pattern
      const pattern = await vscode.window.showInputBox({
        prompt: "Search pattern (literal or /regex/flags)",
        placeHolder: "e.g.  old_name  or  /old_name/gi",
        validateInput: (v) => (v.trim() ? undefined : "Pattern cannot be empty"),
      });
      if (!pattern) return;

      // Step 2: gather replacement
      const replacement = await vscode.window.showInputBox({
        prompt: "Replace with",
        placeHolder: "replacement text (use $1, $2 … for regex groups)",
        value: "",
      });
      if (replacement === undefined) return; // cancelled

      // Step 3: gather optional glob filter
      const globInput = await vscode.window.showInputBox({
        prompt: "File filter glob (leave blank for **/*)",
        placeHolder: "e.g. **/*.py  or  src/**/*.ts",
        value: "",
      });
      if (globInput === undefined) return; // cancelled
      const globPattern = globInput.trim() || "**/*";

      // Parse /pattern/flags syntax
      const regexMatch = pattern.match(/^\/(.+)\/([gimsuy]*)$/);
      const isRegex = Boolean(regexMatch);
      const rawPattern = regexMatch ? regexMatch[1] : pattern;
      const rawFlags = regexMatch ? regexMatch[2] : "";
      const caseSensitive = !rawFlags.includes("i");
      const multiline = rawFlags.includes("s");

      // Validate regex before scanning files
      try {
        buildRegExp({ pattern: rawPattern, replacement, isRegex, caseSensitive, multiline });
      } catch (err) {
        vscode.window.showErrorMessage(
          `Invalid regex: ${err instanceof Error ? err.message : String(err)}`
        );
        return;
      }

      const workspaceRoot = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
      if (!workspaceRoot) {
        vscode.window.showWarningMessage("Open a workspace folder before running smart search/replace.");
        return;
      }

      // Collect matching workspace URIs
      const uris = await listWorkspaceUris(globPattern);
      if (uris.length === 0) {
        vscode.window.showInformationMessage(`No files match "${globPattern}".`);
        return;
      }

      const byRelative = new Map<string, vscode.Uri>();
      for (const uri of uris) {
        const rel = path.relative(workspaceRoot, uri.fsPath).replace(/\\/g, "/");
        byRelative.set(rel, uri);
      }

      // Include generated/proposed candidates in default all-files scope.
      if (globPattern === "**/*") {
        for (const rel of provider.getGeneratedFiles().keys()) {
          if (rel.startsWith("__prev__") || byRelative.has(rel)) {
            continue;
          }
          const absPath = path.join(workspaceRoot, rel);
          if (fs.existsSync(absPath)) {
            byRelative.set(rel, vscode.Uri.file(absPath));
          }
        }
        for (const rel of provider.getProposedFiles().keys()) {
          if (rel.startsWith("__prev__") || byRelative.has(rel)) {
            continue;
          }
          const absPath = path.join(workspaceRoot, rel);
          if (fs.existsSync(absPath)) {
            byRelative.set(rel, vscode.Uri.file(absPath));
          }
        }
      }

      const relativePaths = [...byRelative.keys()];
      const resolvedFiles = await readFiles(relativePaths, {
        proposedFiles: provider.getProposedFiles(),
        generatedFiles: provider.getGeneratedFiles(),
        workspaceRoot,
      });

      const filesForSearch: ResolvedFileForSearch[] = resolvedFiles.map((resolved) => ({
        ...resolved,
        uri: byRelative.get(resolved.relativePath),
      }));

      await searchReplaceAcrossResolvedFiles(filesForSearch, {
        pattern: rawPattern,
        replacement,
        isRegex,
        caseSensitive,
        multiline,
      }, 1);
    })
  );

  // Discard all — delete all written files and close diff tabs
  context.subscriptions.push(
    vscode.commands.registerCommand("jameWorkflow.discardAll", async () => {
      const workspaceRoot = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
      for (const group of vscode.window.tabGroups.all) {
        for (const tab of group.tabs) {
          const input = tab.input as { original?: vscode.Uri; modified?: vscode.Uri } | undefined;
          if (input?.original?.scheme === "jame-proposed") {
            // The modified side is the real workspace file — delete it
            const modUri = input?.modified;
            if (modUri && modUri.scheme === "file" && workspaceRoot) {
              const dest = modUri.fsPath;
              if (fs.existsSync(dest)) {
                fs.unlinkSync(dest);
              }
            }
            await vscode.window.tabGroups.close(tab);
          }
        }
      }
    })
  );
}

export function deactivate(): void {}
