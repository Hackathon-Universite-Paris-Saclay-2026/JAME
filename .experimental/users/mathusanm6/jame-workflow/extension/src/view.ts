import * as fs from "node:fs";
import * as path from "node:path";
import { spawn, type ChildProcess } from "node:child_process";
import * as vscode from "vscode";

type StartRunRequest = {
  userRequest: string;
};

export class JameViewProvider implements vscode.WebviewViewProvider {
  private view?: vscode.WebviewView;
  private backendProcess?: ChildProcess;
  private backendStartupPromise?: Promise<void>;
  private currentRunId?: string;

  constructor(private readonly extensionUri: vscode.Uri) {}

  public resolveWebviewView(
    webviewView: vscode.WebviewView,
    _context: vscode.WebviewViewResolveContext,
    _token: vscode.CancellationToken
  ): void {
    this.view = webviewView;
    webviewView.webview.options = {
      enableScripts: true,
      localResourceRoots: [this.extensionUri],
    };

    webviewView.webview.html = this.getHtml();

    webviewView.webview.onDidReceiveMessage(async (message: unknown) => {
      const msg = message as {
        command?: string;
        userRequest?: string;
        files?: string[];
        projectDir?: string;
        filePath?: string;
        fileContent?: string;
        runId?: string;
        destDir?: string;
      };

      if (msg.command === "openGeneratedFiles") {
        await this.openGeneratedFiles(msg.files ?? [], msg.projectDir);
        return;
      }

      if (msg.command === "openFileDiff") {
        await this.openFileDiff(msg.filePath!, msg.fileContent!);
        return;
      }

      if (msg.command === "saveFiles") {
        await this.saveFilesToWorkspace(msg.files ?? [], msg.projectDir, msg.destDir);
        return;
      }

      if (msg.command === "cancelRun") {
        await this.cancelRun(msg.runId!);
        return;
      }

      if (msg.command === "undoFiles") {
        await this.deleteGeneratedFiles(msg.files ?? [], msg.projectDir);
        return;
      }

      if (msg.command !== "startRun") {
        return;
      }

      const payload = msg as StartRunRequest & { command: string };
      const backendUrl = vscode.workspace
        .getConfiguration()
        .get<string>("jameWorkflow.backendUrl", "http://localhost:8000");

      try {
        await this.ensureBackendReady(backendUrl);

        const fetchFn = (globalThis as { fetch?: (input: string, init?: unknown) => Promise<any> }).fetch;
        if (!fetchFn) {
          throw new Error("Global fetch is unavailable in this VS Code runtime.");
        }

        const resp = await fetchFn(`${backendUrl}/runs`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ user_request: payload.userRequest, max_iterations: 3 }),
        });

        if (!resp.ok) {
          const text = await resp.text();
          throw new Error(`Run creation failed: ${text}`);
        }

        const data = (await resp.json()) as { run_id: string };
        this.currentRunId = data.run_id;

        if (this.view) {
          this.view.webview.postMessage({
            command: "runCreated",
            runId: data.run_id,
            backendUrl,
          });
        }
      } catch (error) {
        if (this.view) {
          this.view.webview.postMessage({
            command: "error",
            message: error instanceof Error ? error.message : String(error),
          });
        }
      }
    });
  }

  public focus(): void {
    if (this.view) {
      this.view.show?.(true);
    }
  }

  public dispose(): void {
    if (this.backendProcess && !this.backendProcess.killed) {
      this.backendProcess.kill();
    }
  }

  private async deleteGeneratedFiles(files: string[], projectDir?: string): Promise<void> {
    // Delete individual files
    for (const f of files) {
      try {
        if (fs.existsSync(f)) fs.unlinkSync(f);
      } catch { /* ignore */ }
    }
    // If project dir is now empty, remove it too
    if (projectDir && fs.existsSync(projectDir)) {
      try {
        const remaining = fs.readdirSync(projectDir);
        if (remaining.length === 0) {
          fs.rmdirSync(projectDir);
        }
      } catch { /* ignore */ }
    }
  }

  private async cancelRun(runId: string): Promise<void> {
    const backendUrl = vscode.workspace
      .getConfiguration()
      .get<string>("jameWorkflow.backendUrl", "http://localhost:8000");

    const fetchFn = (globalThis as { fetch?: (input: string, init?: unknown) => Promise<any> }).fetch;
    if (!fetchFn) return;

    try {
      await fetchFn(`${backendUrl}/runs/${runId}/cancel`, { method: "POST" });
    } catch {
      // ignore
    }
  }

  private async openFileDiff(filePath: string, newContent: string): Promise<void> {
    // Create a virtual document for the new content
    const fileName = path.basename(filePath);
    const ext = path.extname(filePath).slice(1) || "txt";

    // Write to a temp file to diff against
    const tmpDir = path.join(require("os").tmpdir(), "jame-diff");
    fs.mkdirSync(tmpDir, { recursive: true });
    const tmpFile = path.join(tmpDir, fileName);
    fs.writeFileSync(tmpFile, newContent, "utf8");

    // Check if file exists in workspace
    const workspaceRoot = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
    if (workspaceRoot) {
      const existingPath = path.join(workspaceRoot, filePath);
      if (fs.existsSync(existingPath)) {
        // Show diff
        await vscode.commands.executeCommand(
          "vscode.diff",
          vscode.Uri.file(existingPath),
          vscode.Uri.file(tmpFile),
          `${fileName} (JAME generated)`
        );
        return;
      }
    }

    // No existing file — just open the new content
    const doc = await vscode.workspace.openTextDocument(vscode.Uri.file(tmpFile));
    await vscode.window.showTextDocument(doc, { preview: true });
  }

  private async saveFilesToWorkspace(
    absolutePaths: string[],
    projectDir: string | undefined,
    destDir: string | undefined
  ): Promise<void> {
    const workspaceRoot = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;

    if (!workspaceRoot) {
      // Ask user to pick a folder
      const picked = await vscode.window.showOpenDialog({
        canSelectFiles: false,
        canSelectFolders: true,
        canSelectMany: false,
        openLabel: "Save generated files here",
      });
      if (!picked || picked.length === 0) return;
      const dest = picked[0].fsPath;
      await this.copyFiles(absolutePaths, projectDir, dest);
      vscode.window.showInformationMessage(`Files saved to ${dest}`);
      return;
    }

    const target = destDir || workspaceRoot;
    await this.copyFiles(absolutePaths, projectDir, target);

    if (this.view) {
      this.view.webview.postMessage({ command: "filesSaved", destDir: target });
    }

    const open = await vscode.window.showInformationMessage(
      `Generated files saved to ${target}`,
      "Open in Explorer"
    );
    if (open === "Open in Explorer") {
      await vscode.commands.executeCommand("revealInExplorer", vscode.Uri.file(target));
    }
  }

  private async copyFiles(
    absolutePaths: string[],
    projectDir: string | undefined,
    dest: string
  ): Promise<void> {
    fs.mkdirSync(dest, { recursive: true });

    for (const absPath of absolutePaths) {
      if (!fs.existsSync(absPath)) continue;

      let relPath = absPath;
      if (projectDir && absPath.startsWith(projectDir)) {
        relPath = absPath.slice(projectDir.length).replace(/^[/\\]/, "");
      } else {
        relPath = path.basename(absPath);
      }

      const destPath = path.join(dest, relPath);
      fs.mkdirSync(path.dirname(destPath), { recursive: true });
      fs.copyFileSync(absPath, destPath);
    }
  }

  private async openGeneratedFiles(files: string[], projectDir?: string): Promise<void> {
    const existing = files.filter((f) => fs.existsSync(f));

    for (const file of existing.slice(0, 4)) {
      const doc = await vscode.workspace.openTextDocument(vscode.Uri.file(file));
      await vscode.window.showTextDocument(doc, { preview: false, preserveFocus: true });
    }

    if (projectDir && fs.existsSync(projectDir)) {
      const projectUri = vscode.Uri.file(projectDir);
      await vscode.commands.executeCommand("revealInExplorer", projectUri);
    }

    if (existing.length === 0 && projectDir) {
      vscode.window.showInformationMessage(`Build artifacts saved to ${projectDir}`);
    }
  }

  private async ensureBackendReady(backendUrl: string): Promise<void> {
    if (await this.isBackendHealthy(backendUrl)) {
      return;
    }

    const parsed = new URL(backendUrl);
    const localHosts = new Set(["localhost", "127.0.0.1", "0.0.0.0"]);
    if (!localHosts.has(parsed.hostname)) {
      throw new Error(`Backend is unreachable at ${backendUrl}. Start it manually for non-local hosts.`);
    }

    if (!this.backendStartupPromise) {
      this.backendStartupPromise = this.startBackend(backendUrl);
    }

    await this.backendStartupPromise;
  }

  private async startBackend(backendUrl: string): Promise<void> {
    this.view?.webview.postMessage({ command: "system", message: "Starting backend..." });

    const extensionDir = this.extensionUri.fsPath;
    const repoRoot = path.resolve(extensionDir, "..");
    const backendDir = path.join(repoRoot, "backend");
    const venvPython = path.join(backendDir, ".venv", "bin", "python");
    const pythonCmd = fs.existsSync(venvPython) ? venvPython : "python3";

    this.backendProcess = spawn(pythonCmd, ["run_api.py"], {
      cwd: backendDir,
      stdio: "ignore",
      detached: false,
    });

    const timeoutMs = 20000;
    const startedAt = Date.now();
    let delay = 500;

    while (Date.now() - startedAt < timeoutMs) {
      if (await this.isBackendHealthy(backendUrl)) {
        this.view?.webview.postMessage({ command: "system", message: "Backend ready." });
        return;
      }
      await new Promise((resolve) => setTimeout(resolve, delay));
      delay = Math.min(delay * 1.5, 2000);
    }

    throw new Error("Backend did not become ready in time.");
  }

  private async isBackendHealthy(backendUrl: string): Promise<boolean> {
    const fetchFn = (globalThis as { fetch?: (input: string, init?: unknown) => Promise<any> }).fetch;
    if (!fetchFn) {
      return false;
    }

    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 1500);

    try {
      const resp = await fetchFn(`${backendUrl}/health`, { signal: controller.signal });
      return resp.ok;
    } catch {
      return false;
    } finally {
      clearTimeout(timer);
    }
  }

  private getHtml(): string {
    return `<!doctype html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <style>
    *, *::before, *::after { margin: 0; padding: 0; box-sizing: border-box; }

    html, body {
      width: 100%;
      height: 100%;
      overflow: hidden;
    }

    body {
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Helvetica Neue', sans-serif;
      font-size: 13px;
      background: var(--vscode-sideBar-background, #1e1e1e);
      color: var(--vscode-foreground, #cccccc);
      display: flex;
      flex-direction: column;
    }

    /* ── Layout ──────────────────────────────────────────────────── */
    .shell {
      display: flex;
      flex-direction: column;
      height: 100vh;
    }

    .feed {
      flex: 1;
      overflow-y: auto;
      padding: 8px 0;
      scroll-behavior: smooth;
    }

    .feed::-webkit-scrollbar { width: 6px; }
    .feed::-webkit-scrollbar-track { background: transparent; }
    .feed::-webkit-scrollbar-thumb { background: #464647; border-radius: 3px; }
    .feed::-webkit-scrollbar-thumb:hover { background: #5a5a5a; }

    /* ── Empty state ─────────────────────────────────────────────── */
    .empty {
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      gap: 10px;
      padding: 40px 24px;
      text-align: center;
      height: 100%;
      color: #6a6a6a;
    }

    .empty-icon {
      width: 32px;
      height: 32px;
      opacity: 0.4;
    }

    .empty h2 {
      font-size: 13px;
      font-weight: 600;
      color: #909090;
    }

    .empty p {
      font-size: 12px;
      line-height: 1.6;
      color: #6a6a6a;
      max-width: 240px;
    }

    /* ── User message bubble ─────────────────────────────────────── */
    .user-msg {
      display: flex;
      justify-content: flex-end;
      padding: 6px 16px;
    }

    .user-bubble {
      background: #0e639c;
      color: #fff;
      border-radius: 12px 12px 2px 12px;
      padding: 8px 12px;
      font-size: 12px;
      line-height: 1.5;
      max-width: 85%;
      word-break: break-word;
    }

    /* ── Timeline row ────────────────────────────────────────────── */
    .tl-row {
      display: flex;
      align-items: flex-start;
      gap: 8px;
      padding: 3px 12px;
      animation: fadeSlideIn 0.2s ease forwards;
    }

    .tl-badge {
      font-size: 9px;
      font-weight: 700;
      letter-spacing: 0.05em;
      text-transform: uppercase;
      padding: 2px 5px;
      border-radius: 3px;
      flex-shrink: 0;
      margin-top: 1px;
      min-width: 64px;
      text-align: center;
    }

    .badge-architect   { background: #1a2a3f; color: #7ec0f0; border: 1px solid #2a4a6f; }
    .badge-developer   { background: #1f2a1f; color: #7ece7e; border: 1px solid #2f4f2f; }
    .badge-delivery    { background: #2a2a1a; color: #d4c26a; border: 1px solid #4f4f1f; }
    .badge-qa          { background: #2a1a2a; color: #c07ec0; border: 1px solid #4f1f4f; }
    .badge-system      { background: transparent; color: #5a5a5a; border: 1px solid #3a3a3a; }

    .tl-text {
      flex: 1;
      font-size: 12px;
      color: #c5c5c5;
      line-height: 1.5;
    }

    .tl-thinking-toggle {
      font-size: 10px;
      color: #5a5a7a;
      cursor: pointer;
      padding: 1px 5px;
      border-radius: 2px;
      border: 1px solid #3a3a5a;
      background: transparent;
      flex-shrink: 0;
      transition: color 0.15s, border-color 0.15s;
    }
    .tl-thinking-toggle:hover { color: #9090c0; border-color: #6060a0; }
    .tl-thinking-toggle.open  { color: #7ec0f0; border-color: #0e639c; }

    /* ── Thinking block ──────────────────────────────────────────── */
    .thinking-block {
      margin: 2px 12px 4px calc(12px + 64px + 8px);
      border: 1px solid #2a2a4a;
      border-radius: 4px;
      overflow: hidden;
      display: none;
    }
    .thinking-block.shown { display: block; }
    .thinking-header {
      padding: 3px 8px;
      background: #14142a;
      font-size: 10px;
      font-weight: 600;
      color: #7ec0f0;
      letter-spacing: 0.05em;
      text-transform: uppercase;
    }
    .thinking-content {
      padding: 5px 8px;
      font-size: 11px;
      font-family: 'SF Mono', 'Cascadia Code', Consolas, monospace;
      color: #7a7a9a;
      background: #0d0d1e;
      white-space: pre-wrap;
      word-break: break-word;
      max-height: 180px;
      overflow-y: auto;
      line-height: 1.5;
    }

    /* ── Iteration separator ─────────────────────────────────────── */
    .iter-sep {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 6px 12px 4px;
    }
    .iter-sep-line {
      flex: 1;
      height: 1px;
      background: #3e3e42;
    }
    .iter-sep-label {
      font-size: 10px;
      font-weight: 600;
      color: #6a6a6a;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      white-space: nowrap;
    }

    /* ── File diff row ───────────────────────────────────────────── */
    .file-diff-row {
      display: flex;
      align-items: center;
      gap: 6px;
      padding: 2px 12px 2px calc(12px + 64px + 8px);
      font-size: 11px;
    }
    .file-diff-name {
      color: #cccccc;
      flex: 1;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      cursor: pointer;
    }
    .file-diff-name:hover { color: #7ec0f0; text-decoration: underline; }
    .diff-added   { color: #4a9d4a; font-family: monospace; font-size: 10px; }
    .diff-removed { color: #f48771; font-family: monospace; font-size: 10px; }
    .diff-ext {
      font-family: monospace;
      font-size: 9px;
      padding: 1px 3px;
      border-radius: 2px;
      background: #2d2d30;
      color: #9d9d9d;
      flex-shrink: 0;
    }

    /* ── Inline review card ──────────────────────────────────────── */
    .review-card {
      margin: 6px 12px;
      border: 1px solid #2a4a6a;
      border-radius: 5px;
      background: #141e2a;
      overflow: hidden;
    }
    .review-card-header {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 7px 12px;
      background: #1a2a3a;
      border-bottom: 1px solid #2a3a4a;
    }
    .review-card-dot {
      width: 7px; height: 7px; border-radius: 50%;
      background: #4a9d4a; flex-shrink: 0;
      animation: pulse 1.4s ease-in-out infinite;
    }
    .review-card.resolved .review-card-dot { animation: none; background: #6a6a6a; }
    .review-card.kept .review-card-dot     { animation: none; background: #4a9d4a; }
    .review-card.undone .review-card-dot   { animation: none; background: #d7ba7d; }
    .review-card-title  { font-size: 11px; font-weight: 600; color: #7ec0f0; flex: 1; }
    .review-card-sub    { font-size: 10px; color: #5a8abf; }
    .review-card-body   { padding: 8px 12px; }
    .review-file-list   { display: flex; flex-direction: column; gap: 3px; margin-bottom: 9px; }
    .review-file-entry  {
      display: flex; align-items: center; gap: 6px;
      font-size: 11px; color: #c5c5c5; padding: 1px 0;
    }
    .review-actions { display: flex; gap: 6px; }

    /* ── Verdict / actions bar ───────────────────────────────────── */
    .verdict {
      margin: 4px 12px 6px;
      padding: 7px 12px;
      border-radius: 4px;
      font-size: 12px;
      font-weight: 500;
    }
    .verdict.pass { background: #1d3f1d; border: 1px solid #3a8a3a; color: #c3e6c3; }
    .verdict.fail { background: #3f1d1d; border: 1px solid #8a3a3a; color: #e6c3c3; }

    .actions-bar {
      display: flex; flex-direction: column; gap: 6px;
      margin: 6px 12px;
      padding: 10px 12px;
      background: #252526;
      border: 1px solid #3e3e42;
      border-radius: 5px;
    }
    .actions-bar-title {
      font-size: 10px; font-weight: 600; color: #9d9d9d;
      letter-spacing: 0.04em; text-transform: uppercase; margin-bottom: 2px;
    }
    .actions-row { display: flex; gap: 6px; flex-wrap: wrap; }

    /* ── Buttons ─────────────────────────────────────────────────── */
    .btn {
      padding: 5px 11px; font-size: 11px; font-weight: 500;
      border-radius: 4px; cursor: pointer; border: 1px solid transparent;
      transition: background 0.15s, border-color 0.15s; white-space: nowrap;
    }
    .btn-primary  { background: #0e639c; color: #fff; border-color: #0e639c; }
    .btn-primary:hover  { background: #1177bb; }
    .btn-secondary { background: transparent; color: #cccccc; border-color: #4e4e52; }
    .btn-secondary:hover { background: #2d2d30; }
    .btn-danger   { background: transparent; color: #f48771; border-color: #5a3030; }
    .btn-danger:hover   { background: #3a2020; border-color: #f48771; }
    .btn-success  { background: #2d6a2d; color: #c3e6c3; border-color: #3a8a3a; }
    .btn-success:hover  { background: #3a8a3a; }
    .btn:disabled { opacity: 0.5; cursor: not-allowed; }

    /* ── System messages ─────────────────────────────────────────── */
    .sys-msg { padding: 2px 12px; font-size: 11px; color: #6a6a6a; }
    .sys-msg.ok   { color: #4a9d4a; }
    .sys-msg.warn { color: #d7ba7d; }
    .sys-msg.err  { color: #f48771; }

    /* ── File card (legacy, kept for safety) ────────────────────── */
    .file-card {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 5px 8px;
      background: #1e1e1e;
      border: 1px solid #3e3e42;
      border-radius: 4px;
      font-size: 11px;
      cursor: pointer;
      transition: border-color 0.15s, background 0.15s;
    }

    .file-card:hover { border-color: #6a9cbf; background: #252526; }

    .file-icon {
      font-family: 'SF Mono', Consolas, monospace;
      font-size: 9px;
      padding: 1px 4px;
      border-radius: 2px;
      background: #2d2d30;
      color: #9d9d9d;
      flex-shrink: 0;
      letter-spacing: 0.03em;
    }

    .file-path {
      flex: 1;
      color: #cccccc;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .file-action {
      font-size: 10px;
      color: #6a9cbf;
      flex-shrink: 0;
    }

    /* ── QA verdict banner ───────────────────────────────────────── */
    .verdict {
      margin: 4px 12px 8px;
      padding: 8px 12px;
      border-radius: 4px;
      font-size: 12px;
      font-weight: 500;
    }

    .verdict.pass {
      background: #1d3f1d;
      border: 1px solid #3a8a3a;
      color: #c3e6c3;
    }

    .verdict.fail {
      background: #3f1d1d;
      border: 1px solid #8a3a3a;
      color: #e6c3c3;
    }

    /* ── Progress bar ────────────────────────────────────────────── */
    .progress-bar-wrap {
      height: 2px;
      background: #2d2d30;
      overflow: hidden;
      flex-shrink: 0;
    }

    .progress-bar {
      height: 100%;
      background: #0e639c;
      width: 0%;
      transition: width 0.4s ease;
    }

    .progress-bar.indeterminate {
      width: 40%;
      animation: slide 1.6s ease-in-out infinite;
    }

    @keyframes slide {
      0%   { transform: translateX(-100%); }
      100% { transform: translateX(350%); }
    }

    @keyframes pulse {
      0%, 100% { opacity: 1; }
      50% { opacity: 0.4; }
    }

    /* ── Input area ──────────────────────────────────────────────── */
    .input-area {
      border-top: 1px solid #3e3e42;
      padding: 10px 12px;
      flex-shrink: 0;
    }

    .input-row {
      display: flex;
      align-items: flex-end;
      gap: 6px;
      background: #2d2d30;
      border: 1px solid #3e3e42;
      border-radius: 6px;
      padding: 6px 10px;
      transition: border-color 0.15s;
    }

    .input-row:focus-within { border-color: #0e639c; }

    textarea {
      flex: 1;
      min-height: 20px;
      max-height: 120px;
      font-size: 12px;
      background: transparent;
      color: #d4d4d4;
      border: none;
      outline: none;
      resize: none;
      font-family: inherit;
      line-height: 1.5;
    }

    textarea::placeholder { color: #6a6a6a; }
    textarea:disabled { opacity: 0.5; cursor: not-allowed; }

    .send-btn {
      width: 28px;
      height: 28px;
      border-radius: 5px;
      background: #0e639c;
      border: none;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
      flex-shrink: 0;
      transition: background 0.15s;
    }

    .send-btn:hover:not(:disabled) { background: #1177bb; }
    .send-btn:disabled { background: #3a3a3a; cursor: not-allowed; }

    .send-btn svg { width: 14px; height: 14px; fill: #fff; }

    .stop-btn {
      width: 28px;
      height: 28px;
      border-radius: 5px;
      background: #5a2020;
      border: 1px solid #8a3a3a;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
      flex-shrink: 0;
      transition: background 0.15s;
    }

    .stop-btn:hover { background: #7a2a2a; }
    .stop-btn svg { width: 10px; height: 10px; fill: #f48771; }

    .hint {
      margin-top: 5px;
      font-size: 10px;
      color: #5a5a5a;
      text-align: center;
    }

    /* ── Animations ──────────────────────────────────────────────── */
    @keyframes fadeSlideIn {
      from { opacity: 0; transform: translateY(6px); }
      to   { opacity: 1; transform: translateY(0); }
    }

    .fade-in { animation: fadeSlideIn 0.25s ease forwards; }
  </style>
</head>
<body>
  <div class="shell">
    <div class="progress-bar-wrap">
      <div class="progress-bar" id="progressBar"></div>
    </div>

    <div class="feed" id="feed">
      <div class="empty" id="emptyState">
        <svg class="empty-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
          <path stroke-linecap="round" stroke-linejoin="round"
            d="M9.75 3.104v5.714a2.25 2.25 0 0 1-.659 1.591L5 14.5M9.75 3.104c-.251.023-.501.05-.75.082m.75-.082a24.301 24.301 0 0 1 4.5 0m0 0v5.714c0 .597.237 1.17.659 1.591L19.8 15.3M14.25 3.104c.251.023.501.05.75.082M19.8 15.3l-1.57.393A9.065 9.065 0 0 1 12 15a9.065 9.065 0 0 0-6.23-.693L5 14.5m14.8.8 1.402 1.402c1.232 1.232.65 3.318-1.067 3.611A48.309 48.309 0 0 1 12 21a48.25 48.25 0 0 1-8.135-.687c-1.718-.293-2.3-2.379-1.067-3.61L5 14.5" />
        </svg>
        <h2>JAME Orchestrator</h2>
        <p>Describe what you want to build. The multi-agent pipeline will architect, code, and validate it.</p>
      </div>
    </div>

    <div class="input-area">
      <div class="input-row">
        <textarea id="input" rows="1" placeholder="Describe what to build..." spellcheck="false"></textarea>
        <button class="stop-btn" id="stopBtn" style="display:none" title="Stop generation">
          <svg viewBox="0 0 10 10"><rect x="1" y="1" width="8" height="8"/></svg>
        </button>
        <button class="send-btn" id="sendBtn" title="Build (Ctrl+Enter)">
          <svg viewBox="0 0 16 16"><path d="M1 1l14 7L1 15V9l10-2L1 7V1z"/></svg>
        </button>
      </div>
      <div class="hint">Ctrl+Enter to send</div>
    </div>
  </div>

  <script>
    const vscode = acquireVsCodeApi();
    const feed    = document.getElementById('feed');
    const inputEl = document.getElementById('input');
    const sendBtn = document.getElementById('sendBtn');
    const stopBtn = document.getElementById('stopBtn');
    const progressBar = document.getElementById('progressBar');

    let currentRunId     = null;
    let ws               = null;
    let isRunning        = false;
    let generatedFiles   = [];
    let projectDir       = null;
    let currentIteration = 0;   // incremented each time QA→Dev loop fires
    let lastAgent        = null; // tracks agent transitions for separators
    let reviewCardFiles  = [];
    let reviewCardProjectDir = null;

    // ── Progress helpers ─────────────────────────────────────────
    function setProgressIndeterminate() {
      progressBar.classList.add('indeterminate');
      progressBar.style.width = '';
    }
    function clearProgress() {
      progressBar.classList.remove('indeterminate');
      progressBar.style.width = '0%';
    }

    // ── Auto-resize textarea ─────────────────────────────────────
    inputEl.addEventListener('input', () => {
      inputEl.style.height = 'auto';
      inputEl.style.height = Math.min(inputEl.scrollHeight, 120) + 'px';
    });

    // ── Keyboard ─────────────────────────────────────────────────
    inputEl.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && e.ctrlKey) {
        e.preventDefault();
        send();
      }
    });

    sendBtn.addEventListener('click', send);
    stopBtn.addEventListener('click', stop);

    // ── Running state ─────────────────────────────────────────────
    function setRunning(running) {
      isRunning = running;
      inputEl.disabled = running;
      sendBtn.style.display = running ? 'none' : 'flex';
      stopBtn.style.display = running ? 'flex' : 'none';
      if (running) setProgressIndeterminate();
      else clearProgress();
    }

    // ── Utilities ─────────────────────────────────────────────────
    function escHtml(s) {
      return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
    }
    function removeEmpty() { const e = document.getElementById('emptyState'); if (e) e.remove(); }
    function scrollFeed()  { feed.scrollTop = feed.scrollHeight; }

    function countLines(text) { return text ? text.split('\\n').length : 0; }

    // ── User bubble ───────────────────────────────────────────────
    function addUserMessage(text) {
      const el = document.createElement('div');
      el.className = 'user-msg fade-in';
      el.innerHTML = '<div class="user-bubble">' + escHtml(text) + '</div>';
      removeEmpty();
      feed.appendChild(el);
      scrollFeed();
    }

    // ── System / status messages ─────────────────────────────────
    function addSysMsg(text, type = 'info') {
      const el = document.createElement('div');
      el.className = 'sys-msg ' + type + ' fade-in';
      el.textContent = text;
      feed.appendChild(el);
      scrollFeed();
    }

    // ── Timeline row ──────────────────────────────────────────────
    function badgeClass(agentName) {
      const n = (agentName || '').toLowerCase();
      if (n.includes('architect'))       return 'badge-architect';
      if (n.includes('developer') || n.includes('dev')) return 'badge-developer';
      if (n.includes('delivery'))        return 'badge-delivery';
      if (n.includes('quality') || n.includes('qa'))    return 'badge-qa';
      return 'badge-system';
    }

    function addTlRow(agentName, text, thinking) {
      removeEmpty();
      const row = document.createElement('div');
      row.className = 'tl-row fade-in';

      const badge = document.createElement('span');
      badge.className = 'tl-badge ' + badgeClass(agentName);
      badge.textContent = agentName || 'System';

      const textEl = document.createElement('span');
      textEl.className = 'tl-text';
      textEl.textContent = text;

      row.appendChild(badge);
      row.appendChild(textEl);

      if (thinking) {
        const toggleBtn = document.createElement('button');
        toggleBtn.className = 'tl-thinking-toggle';
        toggleBtn.textContent = 'thinking';
        row.appendChild(toggleBtn);

        const thinkBlock = document.createElement('div');
        thinkBlock.className = 'thinking-block';
        thinkBlock.innerHTML =
          '<div class="thinking-header">Agent Reasoning</div>' +
          '<div class="thinking-content">' + escHtml(thinking) + '</div>';

        toggleBtn.addEventListener('click', () => {
          const shown = thinkBlock.classList.toggle('shown');
          toggleBtn.classList.toggle('open', shown);
          toggleBtn.textContent = shown ? 'hide' : 'thinking';
        });

        feed.appendChild(row);
        feed.appendChild(thinkBlock);
      } else {
        feed.appendChild(row);
      }

      scrollFeed();
    }

    // ── File diff row ─────────────────────────────────────────────
    function addFileDiffRow(filePath, content, language, isRevision) {
      const ext = (filePath.split('.').pop() || 'txt').toLowerCase();
      const added = countLines(content);

      const row = document.createElement('div');
      row.className = 'file-diff-row fade-in';
      row.innerHTML =
        '<span class="diff-ext">' + escHtml(ext) + '</span>' +
        '<span class="file-diff-name">' + escHtml(filePath) + '</span>' +
        '<span class="diff-added">+' + added + '</span>';

      if (isRevision) {
        const delSpan = document.createElement('span');
        delSpan.className = 'diff-removed';
        delSpan.textContent = '~rev';
        row.appendChild(delSpan);
      }

      row.querySelector('.file-diff-name').addEventListener('click', () => {
        vscode.postMessage({ command: 'openFileDiff', filePath, fileContent: content });
      });

      feed.appendChild(row);
      scrollFeed();
    }

    // ── Iteration separator ───────────────────────────────────────
    function addIterSep(label) {
      const sep = document.createElement('div');
      sep.className = 'iter-sep fade-in';
      sep.innerHTML =
        '<div class="iter-sep-line"></div>' +
        '<span class="iter-sep-label">' + escHtml(label) + '</span>' +
        '<div class="iter-sep-line"></div>';
      feed.appendChild(sep);
      scrollFeed();
    }

    // ── Actions bar after completion ──────────────────────────────
    function showActionsBar(files, pDir, qaPassed) {
      const bar = document.createElement('div');
      bar.className = 'actions-bar fade-in';

      const title = document.createElement('div');
      title.className = 'actions-bar-title';
      title.textContent = 'Generated Output';
      bar.appendChild(title);

      // Verdict
      const verdict = document.createElement('div');
      verdict.className = 'verdict ' + (qaPassed ? 'pass' : 'fail');
      verdict.textContent = qaPassed
        ? 'QA passed — code meets quality standards'
        : 'QA did not pass — review issues before using';
      bar.appendChild(verdict);

      const row = document.createElement('div');
      row.className = 'actions-row';

      // Save to workspace
      const saveBtn = document.createElement('button');
      saveBtn.className = 'btn btn-primary';
      saveBtn.textContent = 'Save to workspace';
      saveBtn.addEventListener('click', () => {
        vscode.postMessage({ command: 'saveFiles', files, projectDir: pDir });
        saveBtn.textContent = 'Saved';
        saveBtn.disabled = true;
      });
      row.appendChild(saveBtn);

      // Open files
      if (files.length > 0) {
        const openBtn = document.createElement('button');
        openBtn.className = 'btn btn-secondary';
        openBtn.textContent = 'Open in editor';
        openBtn.addEventListener('click', () => {
          vscode.postMessage({ command: 'openGeneratedFiles', files, projectDir: pDir });
        });
        row.appendChild(openBtn);
      }

      bar.appendChild(row);
      feed.appendChild(bar);
      scrollFeed();
    }

    // ── Inline review card (shown immediately after developer) ────
    function showReviewCard(files, pDir, iteration) {
      reviewCardFiles = files;
      reviewCardProjectDir = pDir;

      const card = document.createElement('div');
      card.className = 'review-card fade-in';
      card.id = 'review-card-iter-' + iteration;

      const header = document.createElement('div');
      header.className = 'review-card-header';

      const icon = document.createElement('div');
      icon.className = 'review-card-icon';

      const title = document.createElement('div');
      title.className = 'review-card-title';
      title.textContent = 'Files generated' + (iteration > 0 ? ' (revision ' + iteration + ')' : '');

      const sub = document.createElement('div');
      sub.className = 'review-card-subtitle';
      sub.textContent = files.length + ' file' + (files.length !== 1 ? 's' : '') + ' · QA running...';

      header.appendChild(icon);
      header.appendChild(title);
      header.appendChild(sub);
      card.appendChild(header);

      const body = document.createElement('div');
      body.className = 'review-card-body';

      // File list with preview links
      const fileRows = document.createElement('div');
      fileRows.className = 'review-card-files';

      // We only have absolute paths here; show basename + preview link
      for (const absPath of files.slice(0, 8)) {
        const name = absPath.split('/').pop() || absPath;
        const ext = name.split('.').pop() || 'file';
        const row = document.createElement('div');
        row.className = 'review-file-row';
        row.innerHTML =
          '<span class="review-file-ext">' + escHtml(ext) + '</span>' +
          '<span class="review-file-path">' + escHtml(name) + '</span>' +
          '<span class="review-file-preview">diff</span>';
        // Preview on click — open the file directly since it's already saved
        row.querySelector('.review-file-preview').addEventListener('click', () => {
          vscode.postMessage({ command: 'openFileDiff', filePath: name, fileContent: '' });
          // Actually just open the file
          vscode.postMessage({ command: 'openGeneratedFiles', files: [absPath], projectDir: pDir });
        });
        fileRows.appendChild(row);
      }

      if (files.length > 8) {
        const more = document.createElement('div');
        more.className = 'review-file-row';
        more.style.color = '#6a6a6a';
        more.textContent = '+ ' + (files.length - 8) + ' more file(s)';
        fileRows.appendChild(more);
      }

      body.appendChild(fileRows);

      // Keep / Undo actions
      const actions = document.createElement('div');
      actions.className = 'review-actions';

      const keepBtn = document.createElement('button');
      keepBtn.className = 'btn btn-success';
      keepBtn.textContent = 'Keep';
      keepBtn.title = 'Accept these files and continue with QA';
      keepBtn.addEventListener('click', () => {
        // Files are already written — just mark as kept
        card.classList.add('resolved', 'kept');
        icon.style.animation = 'none';
        icon.style.background = '#4a9d4a';
        sub.textContent = 'Kept — QA in progress';
        actions.style.display = 'none';
        addSysMsg('Files accepted. QA continuing...', 'ok');
      });

      const undoBtn = document.createElement('button');
      undoBtn.className = 'btn btn-danger';
      undoBtn.textContent = 'Undo';
      undoBtn.title = 'Delete these files from disk';
      undoBtn.addEventListener('click', () => {
        card.classList.add('resolved', 'undone');
        icon.style.animation = 'none';
        icon.style.background = '#d7ba7d';
        sub.textContent = 'Undone — files removed';
        actions.style.display = 'none';
        // Ask backend to delete via undoFiles command (VS Code side handles deletion)
        vscode.postMessage({ command: 'undoFiles', files, projectDir: pDir });
        addSysMsg('Files removed from disk.', 'warn');
      });

      actions.appendChild(keepBtn);
      actions.appendChild(undoBtn);
      body.appendChild(actions);
      card.appendChild(body);

      feed.appendChild(card);
      scrollFeed();
    }

    // ── Send / stop ───────────────────────────────────────────────
    function send() {
      const text = inputEl.value.trim();
      if (!text || isRunning) return;

      // Reset state
      generatedFiles = [];
      projectDir = null;
      reviewCardFiles = [];
      reviewCardProjectDir = null;
      currentIteration = 0;
      lastAgent = null;

      addUserMessage(text);
      inputEl.value = '';
      inputEl.style.height = 'auto';
      setRunning(true);

      vscode.postMessage({ command: 'startRun', userRequest: text });
    }

    function stop() {
      if (!currentRunId) return;
      vscode.postMessage({ command: 'cancelRun', runId: currentRunId });
      addSysMsg('Cancellation requested...', 'warn');
    }

    // ── WebSocket events ──────────────────────────────────────────
    window.addEventListener('message', async (event) => {
      const msg = event.data;

      if (msg.command === 'system') {
        addSysMsg(msg.message, 'info');
        return;
      }

      if (msg.command === 'error') {
        addSysMsg('Error: ' + msg.message, 'err');
        setRunning(false);
        return;
      }

      if (msg.command === 'runCreated') {
        currentRunId = msg.runId;
        addSysMsg('Run started [' + msg.runId.substring(0, 8) + ']', 'info');

        const wsUrl = msg.backendUrl
          .replace('http://', 'ws://')
          .replace('https://', 'wss://') + '/ws/runs/' + msg.runId;

        ws = new WebSocket(wsUrl);

        ws.onmessage = (evt) => {
          const data = JSON.parse(evt.data);
          handleServerEvent(data);
        };

        ws.onclose = () => {
          if (isRunning) {
            setRunning(false);
          }
        };

        ws.onerror = () => {
          addSysMsg('WebSocket error. Is the backend running?', 'err');
          setRunning(false);
        };
      }

      if (msg.command === 'filesSaved') {
        addSysMsg('Files saved to ' + msg.destDir, 'ok');
      }
    });

    const AGENT_PHASES = {
      'Architect':        'INCEPTION',
      'architect':        'INCEPTION',
      'Developer':        'CONSTRUCTION',
      'developer':        'CONSTRUCTION',
      'Delivery':         'CONSTRUCTION',
      'delivery_engineer':'CONSTRUCTION',
      'Quality Engineer': 'CONSTRUCTION / QA',
      'quality_engineer': 'CONSTRUCTION / QA',
      'QA':               'CONSTRUCTION / QA',
    };

    const AGENT_DISPLAY = {
      'architect':         'Architect',
      'developer':         'Developer',
      'delivery_engineer': 'Delivery',
      'quality_engineer':  'Quality Engineer',
    };

    function handleServerEvent(data) {
      const event = data.event;
      const rawAgent = data.agent || '';
      const agentDisplay = AGENT_DISPLAY[rawAgent] || rawAgent || 'Orchestrator';

      if (event === 'run_started') {
        addSysMsg('Orchestration started', 'info');
        return;
      }

      if (event === 'agent_update') {
        const msg = data.message || '';
        const thinking = data.payload && data.payload.thinking ? data.payload.thinking : '';
        if (!msg) return;

        // Detect QA → Developer transition → insert iteration separator
        if (
          lastAgent &&
          lastAgent !== agentDisplay &&
          (lastAgent === 'Quality Engineer' || lastAgent === 'QA') &&
          (agentDisplay === 'Developer')
        ) {
          currentIteration++;
          addIterSep('QA feedback — revision ' + currentIteration);
        }

        lastAgent = agentDisplay;
        addTlRow(agentDisplay, msg, thinking);
        return;
      }

      if (event === 'file_generated') {
        const p = data.payload;
        if (p && p.path) {
          const isRev = currentIteration > 0;
          addFileDiffRow(p.path, p.content || '', p.language || 'text', isRev);
          generatedFiles.push(p.path);
        }
        return;
      }

      if (event === 'files_ready') {
        const p = data.payload || {};
        const files = p.generated_files || [];
        const pDir = p.project_dir || null;
        const iter = p.iteration || currentIteration;
        showReviewCard(files, pDir, iter);
        return;
      }

      if (event === 'run_completed') {
        const p = data.payload || {};
        const files = p.generated_files || [];
        projectDir = p.project_dir || null;
        const qaPassed = !!p.qa_passed;

        showActionsBar(files, projectDir, qaPassed);
        setRunning(false);
        clearProgress();
        if (ws) ws.close();
        return;
      }

      if (event === 'run_failed') {
        const err = (data.payload && data.payload.error) || data.message || 'Unknown error';
        const firstLine = err.split('\\n')[0];
        addSysMsg('Build failed: ' + firstLine, 'err');
        setRunning(false);
        if (ws) ws.close();
        return;
      }

      if (event === 'run_cancelled') {
        addSysMsg('Build cancelled.', 'warn');
        setRunning(false);
        if (ws) ws.close();
        return;
      }
    }

    // Focus on load
    inputEl.focus();
  </script>
</body>
</html>`;
  }
}
