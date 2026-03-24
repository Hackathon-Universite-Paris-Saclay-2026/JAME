import * as fs from "node:fs";
import * as path from "node:path";
import { spawn, type ChildProcess } from "node:child_process";
import * as vscode from "vscode";

type StartRunRequest = {
  userRequest: string;
  mode?: string;
};

export class JameViewProvider implements vscode.WebviewViewProvider {
  private view?: vscode.WebviewView;
  private backendProcess?: ChildProcess;
  private backendStartupPromise?: Promise<void>;
  private currentRunId?: string;
  /** Actual backend URL after port resolution (may differ from configured URL). */
  private resolvedBackendUrl?: string;
  /** Proposed file contents keyed by relative path, for inline diff editor. */
  private proposedFiles: Map<string, string> = new Map();
  /** Output channel that surfaces backend stdout+stderr logs. */
  private outputChannel: vscode.OutputChannel = vscode.window.createOutputChannel("JAME Backend");

  constructor(private readonly extensionUri: vscode.Uri) {}

  /** Returns proposed content for the jame-proposed:// URI scheme. */
  public getProposedContent(filePath: string): string {
    return this.proposedFiles.get(filePath) ?? "";
  }

  /** Open a VS Code diff editor between workspace file and proposed content. */
  private async openProposedChange(filePath: string, content: string): Promise<void> {
    this.proposedFiles.set(filePath, content);

    const workspaceRoot = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
    if (!workspaceRoot) {
      // No workspace — just store in memory, nothing to write
      return;
    }

    const destPath = path.join(workspaceRoot, filePath);

    // Capture the previous content (if any) for the diff "original" side
    const hadExisting = fs.existsSync(destPath);
    const previousContent = hadExisting ? fs.readFileSync(destPath, "utf8") : null;

    // Write immediately so the file is on disk (not lost if VS Code crashes)
    fs.mkdirSync(path.dirname(destPath), { recursive: true });
    fs.writeFileSync(destPath, content, "utf8");

    // Open a diff editor showing what changed (previous ↔ new, or empty ↔ new)
    const proposedUri = vscode.Uri.parse(`jame-proposed:${filePath}`);
    const label = `${path.basename(filePath)} (JAME generated)`;

    if (hadExisting && previousContent !== null) {
      // Store the old content under a "previous" key so the diff LHS shows it
      this.proposedFiles.set(`__prev__${filePath}`, previousContent);
      await vscode.commands.executeCommand(
        "vscode.diff",
        vscode.Uri.parse(`jame-proposed:__prev__${filePath}`),
        vscode.Uri.file(destPath),
        label
      );
    } else {
      // New file — diff against empty to show what was added
      await vscode.commands.executeCommand(
        "vscode.diff",
        vscode.Uri.parse("jame-proposed:__empty__"),
        vscode.Uri.file(destPath),
        label
      );
    }
  }

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

    // Eagerly start the backend as a background task so it's ready on first Send.
    // We defer slightly so the webview JS has time to initialise its message listener.
    {
      const backendUrl = vscode.workspace
        .getConfiguration()
        .get<string>("jameWorkflow.backendUrl", "http://localhost:8000");
      setTimeout(() => {
        this.ensureBackendReady(backendUrl).catch((err) => {
          webviewView.webview.postMessage({
            command: "system",
            message: "Backend startup failed: " + (err instanceof Error ? err.message : String(err)),
          });
        });
      }, 200);
    }

    webviewView.webview.onDidReceiveMessage(async (message: unknown) => {
      const msg = message as {
        command?: string;
        userRequest?: string;
        mode?: string;
        files?: string[];
        paths?: string[];   // optional subset for acceptAll/discardAll
        projectDir?: string;
        filePath?: string;
        fileContent?: string;
        runId?: string;
        destDir?: string;
        answer?: string;
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

      if (msg.command === "openProposedChange") {
        await this.openProposedChange(msg.filePath!, msg.fileContent!);
        return;
      }

      if (msg.command === "submitClarification") {
        const backendUrlForClarify = this.resolvedBackendUrl ?? vscode.workspace
          .getConfiguration()
          .get<string>("jameWorkflow.backendUrl", "http://localhost:8000");
        const fetchFn2 = (globalThis as { fetch?: (input: string, init?: unknown) => Promise<any> }).fetch;
        if (fetchFn2 && msg.runId) {
          try {
            await fetchFn2(`${backendUrlForClarify}/runs/${msg.runId}/clarify`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ answer: msg.answer }),
            });
          } catch { /* ignore */ }
        }
        return;
      }

      if (msg.command === "acceptFile") {
        // File already on disk — just close its diff tab
        await this.closeJameProposedTabs(msg.filePath);
        return;
      }

      if (msg.command === "discardFile") {
        // Delete file from workspace and close its diff tab
        const workspaceRoot = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
        if (workspaceRoot && msg.filePath) {
          const dest = path.join(workspaceRoot, msg.filePath);
          if (fs.existsSync(dest)) { fs.unlinkSync(dest); }
        }
        await this.closeJameProposedTabs(msg.filePath);
        return;
      }

      if (msg.command === "acceptAll") {
        await this.acceptAllProposed(msg.paths);
        return;
      }

      if (msg.command === "discardAll") {
        await this.discardAllProposed(msg.paths);
        return;
      }

      if (msg.command === "showLogs") {
        this.outputChannel.show(true);
        return;
      }

      if (msg.command === "logEvent") {
        this.outputChannel.appendLine(msg.line as string);
        return;
      }

      if (msg.command === "clearLogs") {
        this.outputChannel.clear();
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

        const effectiveUrl = this.resolvedBackendUrl ?? backendUrl;
        const resp = await fetchFn(`${effectiveUrl}/runs`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ user_request: payload.userRequest, max_iterations: 3, mode: payload.mode ?? "senior" }),
        });

        if (!resp.ok) {
          const text = await resp.text();
          let friendly = text;
          try {
            const json = JSON.parse(text);
            // Pydantic validation errors: { detail: [{ msg, loc, ctx }] }
            if (Array.isArray(json.detail)) {
              friendly = json.detail.map((e: { msg?: string; loc?: string[]; ctx?: { min_length?: number } }) => {
                if (e.ctx?.min_length) {
                  return `Prompt too short — at least ${e.ctx.min_length} characters required.`;
                }
                return e.msg ?? JSON.stringify(e);
              }).join(" ");
            } else if (typeof json.detail === "string") {
              friendly = json.detail;
            }
          } catch { /* not JSON, use raw text */ }
          throw new Error(friendly);
        }

        const data = (await resp.json()) as { run_id: string };
        this.currentRunId = data.run_id;

        if (this.view) {
          this.view.webview.postMessage({
            command: "runCreated",
            runId: data.run_id,
            backendUrl: this.resolvedBackendUrl ?? backendUrl,
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
    const backendUrl = this.resolvedBackendUrl ?? vscode.workspace
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

  private async acceptAllProposed(paths?: string[]): Promise<void> {
    // Files are already written to workspace on generation — just close diff tabs
    const toAccept = paths
      ? paths
      : [...this.proposedFiles.keys()].filter(k => !k.startsWith("__"));
    for (const filePath of toAccept) {
      this.proposedFiles.delete(filePath);
      await this.closeJameProposedTabs(filePath);
    }
    if (this.view) {
      this.view.webview.postMessage({ command: "allAccepted" });
    }
    vscode.window.showInformationMessage(`Kept ${toAccept.length} generated file(s).`);
  }

  private async discardAllProposed(paths?: string[]): Promise<void> {
    // Delete specified (or all) written files from workspace then close diff tabs
    const workspaceRoot = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
    const toDiscard = paths
      ? paths
      : [...this.proposedFiles.keys()].filter(k => !k.startsWith("__"));
    let deleted = 0;
    if (workspaceRoot) {
      for (const filePath of toDiscard) {
        const dest = path.join(workspaceRoot, filePath);
        if (fs.existsSync(dest)) { fs.unlinkSync(dest); deleted++; }
        this.proposedFiles.delete(filePath);
        await this.closeJameProposedTabs(filePath);
      }
    }
    if (this.view) {
      this.view.webview.postMessage({ command: "allDiscarded" });
    }
    vscode.window.showInformationMessage(`Discarded ${deleted} generated file(s).`);
  }

  private async closeJameProposedTabs(filterPath?: string): Promise<void> {
    for (const group of vscode.window.tabGroups.all) {
      for (const tab of group.tabs) {
        const input = tab.input as { original?: vscode.Uri; modified?: vscode.Uri } | undefined;
        const isJame =
          input?.modified?.scheme === "jame-proposed" ||
          input?.original?.scheme === "jame-proposed";
        if (!isJame) continue;
        if (filterPath) {
          // Only close tabs related to this specific file
          const modPath = input?.modified?.path?.replace(/^\//, "") ?? "";
          const origPath = input?.original?.path?.replace(/^\//, "") ?? "";
          const fp = filterPath.replace(/^\//, "");
          if (!modPath.includes(fp) && !origPath.includes(fp)) continue;
        }
        await vscode.window.tabGroups.close(tab);
      }
    }
  }

  private async ensureBackendReady(backendUrl: string): Promise<void> {
    if (await this.isBackendHealthy(backendUrl)) {
      if (!this.resolvedBackendUrl) {
        this.resolvedBackendUrl = backendUrl;
        this.view?.webview.postMessage({ command: "backendUrlResolved", backendUrl });
        this.outputChannel.clear();
        this.outputChannel.appendLine(`[JAME] Backend already running at ${backendUrl} (externally managed).`);
        this.outputChannel.appendLine(`[JAME] Logs are in the terminal where you started the backend.`);
        this.outputChannel.appendLine(`[JAME] To see logs here, let the extension manage the backend (stop your manual process).`);
      }
      return;
    }

    const parsed = new URL(backendUrl);
    const localHosts = new Set(["localhost", "127.0.0.1", "0.0.0.0"]);
    if (!localHosts.has(parsed.hostname)) {
      throw new Error(`Backend is unreachable at ${backendUrl}. Start it manually for non-local hosts.`);
    }

    if (!this.backendStartupPromise) {
      // Find a free port starting from the configured one, to support
      // multiple VS Code windows running independent backend instances.
      const basePort = parseInt(parsed.port || "8000", 10);
      const freePort = await this.findFreePort(basePort);
      const resolvedUrl = `${parsed.protocol}//${parsed.hostname}:${freePort}`;
      this.resolvedBackendUrl = resolvedUrl;
      this.backendStartupPromise = this.startBackend(resolvedUrl).catch((err) => {
        // Reset so the next Send attempt tries again rather than re-throwing a stale rejection.
        this.backendStartupPromise = undefined;
        this.resolvedBackendUrl = undefined;
        throw err;
      });
      // Notify webview of the actual URL being used (in case port shifted)
      this.view?.webview.postMessage({ command: "backendUrlResolved", backendUrl: resolvedUrl });
    }

    await this.backendStartupPromise;
  }

  /** Scan ports starting at basePort until we find one with no healthy JAME backend. */
  private async findFreePort(basePort: number): Promise<number> {
    for (let port = basePort; port < basePort + 20; port++) {
      const url = `http://localhost:${port}`;
      if (!(await this.isBackendHealthy(url))) {
        return port;
      }
    }
    return basePort; // fallback: let OS reject the bind
  }

  private async startBackend(backendUrl: string): Promise<void> {
    this.view?.webview.postMessage({ command: "system", message: "Starting backend on " + backendUrl + "…" });

    const extensionDir = this.extensionUri.fsPath;
    const repoRoot = path.resolve(extensionDir, "..");
    // Look for a venv at the repo root (venv/ or .venv/)
    const venvPython =
      [
        path.join(repoRoot, "venv", "bin", "python"),
        path.join(repoRoot, ".venv", "bin", "python"),
      ].find((p) => fs.existsSync(p)) ?? "python3";

    const parsed = new URL(backendUrl);
    const port = parsed.port || "8000";

    // Capture stderr so startup crashes surface a useful error message
    let stderrOutput = "";
    this.outputChannel.clear();
    this.outputChannel.appendLine(`[JAME] Starting backend on ${backendUrl}`);
    this.backendProcess = spawn(
      venvPython,
      ["-u", "-m", "uvicorn", "api.app:app", "--host", "0.0.0.0", "--port", port],
      {
        cwd: repoRoot,
        stdio: ["ignore", "pipe", "pipe"],
        detached: false,
        env: { ...process.env, PYTHONUNBUFFERED: "1" },
      }
    );

    if (this.backendProcess.stdout) {
      this.backendProcess.stdout.on("data", (chunk: Buffer) => {
        this.outputChannel.append(chunk.toString());
      });
    }

    if (this.backendProcess.stderr) {
      this.backendProcess.stderr.on("data", (chunk: Buffer) => {
        const text = chunk.toString();
        stderrOutput += text;
        this.outputChannel.append(text);
        // Keep only the last 2000 chars to avoid unbounded growth
        if (stderrOutput.length > 2000) {
          stderrOutput = stderrOutput.slice(-2000);
        }
      });
    }

    // Detect immediate crash (e.g. import error, bad port)
    const exitPromise = new Promise<number | null>((resolve) => {
      this.backendProcess!.once("exit", (code) => resolve(code));
    });

    const timeoutMs = 60000;
    const startedAt = Date.now();
    let delay = 500;

    while (Date.now() - startedAt < timeoutMs) {
      if (await this.isBackendHealthy(backendUrl)) {
        this.view?.webview.postMessage({ command: "system", message: "Backend ready." });
        return;
      }

      // Check if the process already exited (crash on startup)
      const exitCode = await Promise.race([
        exitPromise,
        new Promise<undefined>((resolve) => setTimeout(() => resolve(undefined), 50)),
      ]);
      if (exitCode !== undefined) {
        const snippet = stderrOutput.trim().split("\n").slice(-10).join("\n");
        throw new Error(
          `Backend process exited (code ${exitCode}) before becoming healthy.\n${snippet}`
        );
      }

      await new Promise((resolve) => setTimeout(resolve, delay));
      delay = Math.min(delay * 1.5, 2000);
    }

    const snippet = stderrOutput.trim().split("\n").slice(-10).join("\n");
    throw new Error(
      `Backend did not become ready in time (${timeoutMs / 1000}s).${snippet ? "\n" + snippet : ""}`
    );
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
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src http://localhost:* ws://localhost:*;" />
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

    /* ── Agent log rows ──────────────────────────────────────────── */
    /* Rows grow to fit content — tag/pill stay top-aligned */
    .agent-row {
      display: flex; align-items: flex-start; gap: 6px;
      padding: 2px 10px;
      font-size: 11.5px;
      animation: fadeSlideIn 0.15s ease forwards;
      border-left: 2px solid transparent;
    }
    .agent-row:hover { background: rgba(255,255,255,0.03); }

    /* left accent by agent */
    .agent-row.ac-architect { border-left-color: #4a9fd4; }
    .agent-row.ac-developer { border-left-color: #4ec94e; }
    .agent-row.ac-delivery  { border-left-color: #d4c24a; }
    .agent-row.ac-qa        { border-left-color: #c04ac0; }
    .agent-row.ac-system    { border-left-color: #555; }

    /* Agent tag — short uppercase label, fixed width, top-aligned */
    .ar-tag {
      font-size: 10px; font-weight: 700; letter-spacing: 0.07em;
      text-transform: uppercase; flex-shrink: 0;
      width: 56px; padding-top: 2px; line-height: 1.4;
    }
    .ac-architect .ar-tag { color: #7ec0f0; }
    .ac-developer .ar-tag { color: #7ece7e; }
    .ac-delivery  .ar-tag { color: #d4c26a; }
    .ac-qa        .ar-tag { color: #c07ec0; }
    .ac-system    .ar-tag { color: #7a7a7a; }
    /* repeated agent on consecutive rows — dim the tag */
    .agent-row.same-agent .ar-tag { color: transparent; }

    /* Phase pill — stays on first line even if message wraps */
    .ar-phase {
      font-size: 9px; font-weight: 600; letter-spacing: 0.05em;
      text-transform: uppercase; padding: 1px 5px; border-radius: 10px;
      flex-shrink: 0; white-space: nowrap; margin-top: 1px; align-self: flex-start;
    }
    .phase-plan        { background: #1a2f4a; color: #7ec0f0; }
    .phase-act         { background: #1a3a1a; color: #7ece7e; }
    .phase-reason      { background: #3a3a10; color: #d4c26a; }
    .phase-design      { background: #1a2f4a; color: #7ec0f0; }
    .phase-validate    { background: #2a1a3a; color: #c07ec0; }
    .phase-self_critique { background: #2a1a3a; color: #c07ec0; }
    .phase-classify    { background: #1a2a2a; color: #7ecfc0; }
    .phase-interrogation { background: #2a2010; color: #d4a46a; }
    .phase-construction { background: #1a3a1a; color: #7ece7e; }
    .phase-learning    { background: #2a1a3a; color: #c07ec0; }
    .phase-other       { background: #252525; color: #7a7a7a; }

    /* Message text — wraps naturally, row height follows content */
    .ar-msg {
      flex: 1; color: #d4d4d4; line-height: 1.5;
      white-space: normal; word-break: break-word;
    }

    /* Thinking toggle button */
    .ar-think-btn {
      flex-shrink: 0; font-size: 9px; color: #5a5a8a;
      cursor: pointer; user-select: none; padding: 1px 5px;
      background: #10102a; border: 1px solid #2a2a4a; border-radius: 3px;
      line-height: 16px; align-self: flex-start; margin-top: 1px; white-space: nowrap;
    }
    .ar-think-btn:hover { color: #9090d0; }

    /* Thinking content — hidden by default, shown below the row */
    .ar-think-content {
      display: none;
      padding: 5px 10px 5px 72px; /* indent to align under message */
      font-size: 11px;
      font-family: 'SF Mono', 'Cascadia Code', Consolas, monospace;
      color: #6a6a9a; background: #0a0a1a;
      white-space: pre-wrap; word-break: break-word;
      max-height: 180px; overflow-y: auto; line-height: 1.5;
      border-left: 2px solid #2a2a5a;
      margin: 0 10px 2px;
      border-radius: 0 0 4px 4px;
      animation: fadeSlideIn 0.15s ease forwards;
    }
    .ar-think-content.shown { display: block; }

    /* Legacy — kept so old addTlRow callers don't crash */
    .tl-row { display: none; }
    .tl-badge { display: none; }
    .tl-text  { display: none; }
    /* Old card classes — hidden */
    .agent-card { display: none; }
    .thinking-block { display: none; }

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

    /* ── Files panel (above input, Copilot-style) ────────────────── */
    .files-panel {
      display: none;
      flex-direction: column;
      border-top: 1px solid #3e3e42;
      background: #1e1e1e;
      flex-shrink: 0;
      max-height: 220px;
    }
    .files-panel.visible { display: flex; }
    .files-panel-header {
      display: flex; align-items: center; justify-content: space-between;
      padding: 5px 12px; gap: 8px;
      border-bottom: 1px solid #2d2d30;
      background: #252526;
      flex-shrink: 0;
    }
    .files-panel-title {
      font-size: 10px; font-weight: 600; color: #9d9d9d;
      text-transform: uppercase; letter-spacing: 0.04em;
    }
    .files-panel-summary { font-size: 10px; color: #5a8abf; }
    .files-panel-actions { display: flex; gap: 5px; }
    .files-panel-body {
      overflow-y: auto; padding: 4px 0; flex: 1;
    }
    .files-panel-body::-webkit-scrollbar { width: 4px; }
    .files-panel-body::-webkit-scrollbar-thumb { background: #464647; border-radius: 2px; }
    .fp-row {
      display: flex; align-items: center; gap: 6px;
      padding: 3px 12px; cursor: pointer; font-size: 11px;
      transition: background 0.1s;
    }
    .fp-row:hover { background: #2a2d2e; }
    .fp-ext {
      font-family: monospace; font-size: 9px; padding: 1px 3px;
      border-radius: 2px; background: #2d2d30; color: #9d9d9d; flex-shrink: 0;
    }
    .fp-name { flex: 1; color: #cccccc; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; cursor: pointer; }
    .fp-name:hover { text-decoration: underline; color: #9cdcfe; }
    .fp-lines { font-family: monospace; font-size: 10px; color: #4a9d4a; flex-shrink: 0; }
    .fp-action {
      font-size: 11px; flex-shrink: 0; cursor: pointer; padding: 0 3px;
      opacity: 0.4; transition: opacity 0.15s;
    }
    .fp-action:hover { opacity: 1; }
    .fp-keep { color: #4ec94e; }
    .fp-undo { color: #f48771; }
    .fp-row.fp-kept    { opacity: 0.55; }
    .fp-row.fp-discarded { opacity: 0.35; text-decoration: line-through; }
    .fp-verdict {
      padding: 4px 12px; font-size: 11px; font-weight: 500;
      border-top: 1px solid #2d2d30;
    }
    .fp-verdict.pass { color: #4a9d4a; }
    .fp-verdict.fail { color: #f48771; }

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

    /* ── New-chat confirm overlay ────────────────────────────────── */
    .nc-overlay {
      display: none;
      position: fixed; inset: 0; z-index: 999;
      background: rgba(0,0,0,0.45);
      align-items: center; justify-content: center;
    }
    .nc-modal {
      background: var(--vscode-sideBar-background, #252526);
      border: 1px solid var(--vscode-widget-border, #3e3e42);
      border-radius: 6px;
      padding: 20px 20px 16px;
      width: 240px;
      box-shadow: 0 4px 24px rgba(0,0,0,0.5);
    }
    .nc-msg {
      margin: 0 0 16px;
      font-size: 12px;
      line-height: 1.6;
      color: var(--vscode-foreground, #cccccc);
      text-align: center;
    }
    .nc-msg strong { color: #f48771; font-weight: 600; }
    .nc-actions {
      display: flex; gap: 8px; justify-content: flex-end;
    }

    /* ── System messages ─────────────────────────────────────────── */
    .sys-msg {
      display: flex; align-items: flex-start; gap: 6px;
      padding: 4px 12px; font-size: 11px; color: #6a6a6a;
      line-height: 1.5;
    }
    .sys-msg-icon { flex-shrink: 0; font-style: normal; }
    .sys-msg-text { flex: 1; word-break: break-word; }
    .sys-msg.ok {
      color: #4a9d4a;
      background: rgba(74,157,74,0.06);
      border-left: 2px solid #4a9d4a;
      border-radius: 0 4px 4px 0;
      padding: 6px 12px 6px 10px;
      margin: 2px 8px;
    }
    .sys-msg.warn {
      color: #d7ba7d;
      background: rgba(215,186,125,0.06);
      border-left: 2px solid #d7ba7d;
      border-radius: 0 4px 4px 0;
      padding: 6px 12px 6px 10px;
      margin: 2px 8px;
    }
    .sys-msg.err  {
      color: #f48771;
      background: rgba(244,135,113,0.07);
      border-left: 2px solid #f48771;
      border-radius: 0 4px 4px 0;
      padding: 6px 12px 6px 10px;
      margin: 2px 8px;
    }

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

    /* ── Phase pills ─────────────────────────────────────────────── */
    .tl-phase {
      font-size: 8px; font-weight: 600; letter-spacing: 0.04em;
      text-transform: uppercase; padding: 1px 4px; border-radius: 2px;
      flex-shrink: 0; margin-top: 2px; opacity: 0.7;
    }
    .phase-plan   { background: #1a2a3f; color: #7ec0f0; }
    .phase-act    { background: #1f2a1f; color: #7ece7e; }
    .phase-reason { background: #2a2a1a; color: #d4c26a; }
    .phase-other  { background: #2a2a2a; color: #7a7a7a; }

    /* ── Clarification card ───────────────────────────────────────── */
    .clarify-card {
      margin: 6px 10px;
      border: 1px solid #3e3e42;
      border-radius: 6px;
      background: #1e1e1e;
      overflow: hidden;
      font-size: 12px;
    }
    .clarify-q {
      padding: 10px 12px 8px;
      font-size: 12px; color: #c5c5c5; line-height: 1.4;
      border-bottom: 1px solid #3e3e42;
    }
    .clarify-opts { display: flex; flex-direction: column; }
    .clarify-opt {
      display: flex; align-items: flex-start; gap: 10px;
      padding: 8px 12px; cursor: pointer;
      border-bottom: 1px solid #2d2d2d;
      transition: background 0.1s;
    }
    .clarify-opt:last-child { border-bottom: none; }
    .clarify-opt:hover { background: #2a2d2e; }
    .clarify-opt.selected { background: #252526; }
    .clarify-opt-num {
      flex-shrink: 0; width: 16px;
      font-size: 11px; color: #6a6a6a; padding-top: 1px;
    }
    .clarify-opt-body { flex: 1; display: flex; flex-direction: column; gap: 2px; }
    .clarify-opt-label { font-size: 12px; font-weight: 600; color: #d4d4d4; }
    .clarify-opt-desc  { font-size: 11px; color: #6a6a6a; }
    .clarify-opt-check {
      flex-shrink: 0; font-size: 13px; color: #4a9d4a;
      visibility: hidden;
    }
    .clarify-opt.selected .clarify-opt-check { visibility: visible; }
    .clarify-footer {
      display: flex; align-items: center; gap: 4px;
      padding: 5px 10px;
      border-top: 1px solid #3e3e42;
      background: #181818;
    }
    .clarify-nav {
      background: none; border: none; color: #6a6a6a;
      cursor: pointer; font-size: 13px; padding: 2px 4px;
      border-radius: 3px; line-height: 1;
    }
    .clarify-nav:hover:not(:disabled) { color: #c5c5c5; background: #2d2d30; }
    .clarify-nav:disabled { opacity: 0.25; cursor: default; }
    .clarify-page { font-size: 10px; color: #6a6a6a; margin-right: auto; }
    .clarify-free-row {
      display: flex; gap: 6px;
      padding: 6px 10px;
      border-top: 1px solid #3e3e42;
    }
    .clarify-text {
      flex: 1; padding: 4px 8px; font-size: 11px; font-family: inherit;
      background: #2d2d30; border: 1px solid #3e3e42; border-radius: 4px;
      color: #d4d4d4; outline: none; resize: none; min-height: 28px; max-height: 60px;
    }
    .clarify-text:focus { border-color: #0e639c; }
    /* Answered: collapses to Q/A summary */
    .clarify-summary {
      margin: 2px 10px;
      padding: 5px 10px;
      border-left: 2px solid #3e3e42;
      font-size: 11px; line-height: 1.5;
    }
    .clarify-summary .cs-q { color: #6a6a6a; }
    .clarify-summary .cs-a { color: #c5c5c5; font-weight: 600; }

    /* ── Slash command suggestion dropdown ──────────────────────── */
    .slash-suggestions {
      display: none;
      position: absolute; bottom: 100%; left: 0; right: 0;
      background: #252526;
      border: 1px solid #3e3e42;
      border-radius: 6px;
      overflow: hidden;
      box-shadow: 0 -4px 16px rgba(0,0,0,0.4);
      z-index: 100;
      margin-bottom: 4px;
    }
    .slash-suggestions.open { display: block; }
    .slash-suggestion-item {
      display: flex; align-items: center; gap: 10px;
      padding: 7px 12px;
      cursor: pointer;
      font-size: 12px;
      transition: background 0.1s;
    }
    .slash-suggestion-item:hover,
    .slash-suggestion-item.active { background: #094771; }
    .slash-suggestion-name {
      color: #569cd6; font-weight: 600; flex-shrink: 0;
    }
    .slash-suggestion-desc {
      color: #6a6a6a; font-size: 11px;
      overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    }

    /* ── Slash pill inside input ─────────────────────────────────── */
    .slash-pill {
      display: none; align-items: center; gap: 4px;
      background: #0e3a5e; border: 1px solid #0e639c;
      border-radius: 4px; padding: 1px 6px 1px 7px;
      font-size: 11px; font-weight: 600; color: #569cd6;
      white-space: nowrap; flex-shrink: 0; user-select: none;
      line-height: 18px;
    }
    .slash-pill.visible { display: flex; }
    .slash-pill-x {
      font-size: 13px; line-height: 1; cursor: pointer;
      color: #4a8abf; margin-left: 1px;
      display: flex; align-items: center;
    }
    .slash-pill-x:hover { color: #7ec0f0; }

    /* ── JAME header (always visible above input) ────────────────── */
    .jame-header {
      display: flex;
      justify-content: center;
      padding: 8px 12px 6px;
      border-top: 1px solid #3e3e42;
      flex-shrink: 0;
    }
    .jame-title { display: flex; flex-direction: column; align-items: center; gap: 6px; }
    .jame-letters { display: flex; gap: 2px; font-size: 18px; font-weight: 700; letter-spacing: 2px; }
    .jame-letters .j { color: #569cd6; }
    .jame-letters .a { color: #9cdcfe; }
    .jame-letters .m { color: #4ec9b0; }
    .jame-letters .e { color: #ce9178; }
    .jame-acronym {
      display: flex; gap: 6px;
      font-size: 9.5px; font-weight: 500;
      letter-spacing: 0.16em; text-transform: uppercase;
      opacity: 0.65;
    }
    .jame-acronym .w-j { color: #569cd6; }
    .jame-acronym .w-a { color: #9cdcfe; }
    .jame-acronym .w-m { color: #4ec9b0; }
    .jame-acronym .w-e { color: #ce9178; }

    /* ── Input area ──────────────────────────────────────────────── */
    .input-area {
      padding: 6px 12px 10px;
      flex-shrink: 0;
      position: relative;
    }

    /* ── Mode badge (top-bar) ─────────────────────────────────────── */
    .mode-badge {
      display: flex; align-items: center; gap: 5px;
      padding: 3px 9px; border-radius: 4px; font-size: 11px; font-weight: 600;
      letter-spacing: 0.03em; cursor: pointer; user-select: none;
      border: 1px solid transparent; transition: background 0.15s, border-color 0.15s;
    }
    .mode-badge:hover:not(.locked) { border-color: #3e3e42; background: #2a2d2e; }
    .mode-badge.locked { cursor: default; opacity: 0.7; }
    .mode-badge.m-junior { color: #4ec94e; }
    .mode-badge.m-senior { color: #569cd6; }
    .mode-badge.m-expert { color: #d4a46a; }
    .mode-badge-icon { font-size: 12px; }
    .mode-badge-lock { font-size: 10px; opacity: 0.6; margin-left: 2px; }

    /* ── Mode sub-suggestion (for /mode) ─────────────────────────── */
    .slash-sub-item {
      display: flex; flex-direction: column; gap: 2px;
      padding: 8px 12px; cursor: pointer; transition: background 0.1s;
      border-left: 2px solid transparent;
    }
    .slash-sub-item:hover,
    .slash-sub-item.active { background: #094771; }
    .slash-sub-item.active { border-left-color: #569cd6; }
    .slash-sub-name { font-size: 12px; font-weight: 600; }
    .slash-sub-name.m-junior { color: #4ec94e; }
    .slash-sub-name.m-senior { color: #569cd6; }
    .slash-sub-name.m-expert { color: #d4a46a; }
    .slash-sub-desc { font-size: 11px; color: #6a6a6a; line-height: 1.4; }

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
      max-height: 160px;
      font-size: 12px;
      background: transparent;
      color: #d4d4d4;
      border: none;
      outline: none;
      resize: none;
      font-family: inherit;
      line-height: 1.5;
      overflow-y: auto;
    }

    textarea::-webkit-scrollbar { width: 4px; }
    textarea::-webkit-scrollbar-track { background: transparent; }
    textarea::-webkit-scrollbar-thumb { background: #464647; border-radius: 2px; }
    textarea::-webkit-scrollbar-thumb:hover { background: #5a5a5a; }

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

    /* ── Top bar ─────────────────────────────────────────────────── */
    .top-bar {
      display: flex;
      align-items: center;
      justify-content: flex-end;
      padding: 4px 8px;
      flex-shrink: 0;
    }

    .new-chat-btn {
      display: flex;
      align-items: center;
      gap: 5px;
      padding: 4px 9px;
      background: transparent;
      border: 1px solid transparent;
      border-radius: 5px;
      color: #6a6a6a;
      cursor: pointer;
      font-size: 11px;
      font-family: inherit;
      font-weight: 500;
      letter-spacing: 0.02em;
      transition: color 0.15s, border-color 0.15s, background 0.15s;
      white-space: nowrap;
    }

    .new-chat-btn:hover {
      color: #cccccc;
      border-color: #3e3e42;
      background: #2a2d2e;
    }

    .new-chat-btn svg {
      width: 13px;
      height: 13px;
      flex-shrink: 0;
      stroke: currentColor;
      fill: none;
    }

    .new-chat-btn:disabled {
      opacity: 0.35;
      cursor: not-allowed;
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
    <div class="top-bar">
      <div id="modeBadge" class="mode-badge m-senior" title="Click to change mode (or type /mode)">
        <span class="mode-badge-icon" id="modeBadgeIcon">&#9670;</span>
        <span id="modeBadgeLabel">Senior</span>
        <span class="mode-badge-lock" id="modeBadgeLock" style="display:none">&#128274;</span>
      </div>
      <button class="new-chat-btn" id="showLogsBtn" title="Show backend logs in terminal">
        <svg viewBox="0 0 16 16" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" fill="none">
          <polyline points="2,4 6,8 2,12"/>
          <line x1="8" y1="12" x2="14" y2="12"/>
        </svg>
        Logs
      </button>
      <button class="new-chat-btn" id="newChatBtn" title="New chat — clear conversation">
        <svg viewBox="0 0 16 16" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
          <line x1="8" y1="2" x2="8" y2="14"/>
          <line x1="2" y1="8" x2="14" y2="8"/>
        </svg>
        New chat
      </button>
    </div>

    <div class="progress-bar-wrap">
      <div class="progress-bar" id="progressBar"></div>
    </div>

    <div class="feed" id="feed">
      <div class="empty" id="emptyState">
        <svg class="empty-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
          <path stroke-linecap="round" stroke-linejoin="round"
            d="M9.75 3.104v5.714a2.25 2.25 0 0 1-.659 1.591L5 14.5M9.75 3.104c-.251.023-.501.05-.75.082m.75-.082a24.301 24.301 0 0 1 4.5 0m0 0v5.714c0 .597.237 1.17.659 1.591L19.8 15.3M14.25 3.104c.251.023.501.05.75.082M19.8 15.3l-1.57.393A9.065 9.065 0 0 1 12 15a9.065 9.065 0 0 0-6.23-.693L5 14.5m14.8.8 1.402 1.402c1.232 1.232.65 3.318-1.067 3.611A48.309 48.309 0 0 1 12 21a48.25 48.25 0 0 1-8.135-.687c-1.718-.293-2.3-2.379-1.067-3.61L5 14.5" />
        </svg>
        <p>Describe what you want to build. The multi-agent pipeline will architect, code, and validate it.</p>
      </div>
    </div>

    <div class="jame-header">
      <div id="jameTitle" class="jame-title">
        <span class="jame-letters"><span class="j">J</span><span class="a">A</span><span class="m">M</span><span class="e">E</span></span>
        <span class="jame-acronym"><span class="w-j">Just</span><span class="w-a">A</span><span class="w-m">Model-Driven</span><span class="w-e">Engineer</span></span>
      </div>
    </div>

    <div class="files-panel" id="filesPanel">
      <div class="files-panel-header">
        <span class="files-panel-title">Files changed</span>
        <span class="files-panel-summary" id="filesPanelSummary"></span>
        <div class="files-panel-actions">
          <button class="btn btn-success" id="fpKeepBtn" style="padding:3px 8px;font-size:10px">Keep All</button>
          <button class="btn btn-danger"  id="fpUndoBtn" style="padding:3px 8px;font-size:10px">Undo All</button>
        </div>
      </div>
      <div class="files-panel-body" id="filesPanelBody"></div>
    </div>

    <div class="input-area">
      <div id="slashSuggestions" class="slash-suggestions"></div>
      <div class="input-row">
        <div id="slashPill" class="slash-pill">
          <span id="slashPillLabel"></span>
          <span class="slash-pill-x" id="slashPillX">&#x2715;</span>
        </div>
        <textarea id="input" rows="1" placeholder="Describe what to build..." spellcheck="false"></textarea>
        <button class="stop-btn" id="stopBtn" style="display:none" title="Stop generation">
          <svg viewBox="0 0 10 10"><rect x="1" y="1" width="8" height="8"/></svg>
        </button>
        <button class="send-btn" id="sendBtn" title="Build (Enter)">
          <svg viewBox="0 0 16 16"><path d="M1 1l14 7L1 15V9l10-2L1 7V1z"/></svg>
        </button>
      </div>
      <div class="hint">Enter to send · Shift+Enter for new line</div>
    </div>
  </div>

  <div id="newChatConfirm" class="nc-overlay">
    <div class="nc-modal">
      <p class="nc-msg">A build is currently running.<br>Starting a new chat will <strong>stop execution</strong>.<br>Continue?</p>
      <div class="nc-actions">
        <button id="newChatConfirmOk"     class="btn btn-danger">Stop &amp; New Chat</button>
        <button id="newChatConfirmCancel" class="btn btn-secondary">Keep Running</button>
      </div>
    </div>
  </div>

  <div id="modeChangeConfirm" class="nc-overlay">
    <div class="nc-modal">
      <p class="nc-msg" id="modeChangeMsg">Switch to <strong id="modeChangeTarget"></strong> mode?<br>This will <strong>clear the current chat</strong>.</p>
      <div class="nc-actions">
        <button id="modeChangeOk"     class="btn btn-primary">Switch &amp; Clear</button>
        <button id="modeChangeCancel" class="btn btn-secondary">Keep Current</button>
      </div>
    </div>
  </div>

  <script>
    const vscode = acquireVsCodeApi();
    const feed         = document.getElementById('feed');
    const inputEl      = document.getElementById('input');
    const sendBtn      = document.getElementById('sendBtn');
    const stopBtn      = document.getElementById('stopBtn');
    const progressBar  = document.getElementById('progressBar');
    const modeSelect   = null; // replaced by modeBadge
    const filesPanel   = document.getElementById('filesPanel');
    const filesPanelBody    = document.getElementById('filesPanelBody');
    const filesPanelSummary = document.getElementById('filesPanelSummary');
    const fpKeepBtn    = document.getElementById('fpKeepBtn');
    const fpUndoBtn    = document.getElementById('fpUndoBtn');
    const showLogsBtn       = document.getElementById('showLogsBtn');
    const newChatBtn        = document.getElementById('newChatBtn');
    const newChatConfirmEl  = document.getElementById('newChatConfirm');
    const newChatConfirmOk  = document.getElementById('newChatConfirmOk');
    const newChatConfirmCancel = document.getElementById('newChatConfirmCancel');
    const slashSuggestionsEl = document.getElementById('slashSuggestions');
    const slashPillEl        = document.getElementById('slashPill');
    const slashPillLabel     = document.getElementById('slashPillLabel');
    const slashPillX         = document.getElementById('slashPillX');
    const modeBadgeEl        = document.getElementById('modeBadge');
    const modeBadgeLabelEl   = document.getElementById('modeBadgeLabel');
    const modeBadgeLockEl    = document.getElementById('modeBadgeLock');
    const modeChangeConfirmEl  = document.getElementById('modeChangeConfirm');
    const modeChangeTargetEl   = document.getElementById('modeChangeTarget');
    const modeChangeOkEl       = document.getElementById('modeChangeOk');
    const modeChangeCancelEl   = document.getElementById('modeChangeCancel');
    let pendingMode = null;

    modeChangeOkEl.addEventListener('click', () => {
      modeChangeConfirmEl.style.display = 'none';
      if (pendingMode) {
        unlockMode();
        applyMode(pendingMode);
        pendingMode = null;
        doNewChat();
      }
    });
    modeChangeCancelEl.addEventListener('click', () => {
      modeChangeConfirmEl.style.display = 'none';
      pendingMode = null;
    });

    // ── Mode state ────────────────────────────────────────────────
    const MODE_META = {
      junior: { label: 'Junior', icon: '&#9671;', cls: 'm-junior',
                desc: 'Learning mode — step-by-step guidance, clarifications, human in the loop at every decision' },
      senior: { label: 'Senior', icon: '&#9670;', cls: 'm-senior',
                desc: 'Collaborative mode — human in the loop, reviews before executing significant changes' },
      expert: { label: 'Expert', icon: '&#9654;', cls: 'm-expert',
                desc: 'Autonomous mode — runs without interruption, only pauses for genuinely dangerous commands' },
    };
    let currentMode = 'senior';
    let modeLocked  = false;

    function applyMode(mode) {
      if (modeLocked) return;
      if (!MODE_META[mode]) return;
      currentMode = mode;
      const m = MODE_META[mode];
      modeBadgeEl.className = 'mode-badge ' + m.cls;
      modeBadgeLabelEl.textContent = m.label;
      modeBadgeEl.title = m.desc;
    }

    function lockMode() {
      modeLocked = true;
      modeBadgeEl.classList.add('locked');
      modeBadgeLockEl.style.display = '';
    }

    function unlockMode() {
      modeLocked = false;
      modeBadgeEl.classList.remove('locked');
      modeBadgeLockEl.style.display = 'none';
    }

    // Badge click → open /mode sub-suggestions (only when unlocked)
    modeBadgeEl.addEventListener('click', () => {
      if (modeLocked) return;
      openModeSuggestions();
      inputEl.focus();
    });

    // ── Slash command registry ────────────────────────────────────
    const SLASH_COMMANDS = [
      { name: '/mode',   desc: 'Switch mode for this chat' },
      { name: '/fix',    desc: 'Ask the agent to fix an issue in generated code' },
      { name: '/explain',desc: 'Explain the last generated code or output' },
      { name: '/retry',  desc: 'Retry the last failed build' },
      { name: '/clear',  desc: 'Clear conversation and start fresh' },
      { name: '/fun',    desc: 'Toggle the spirit of JAME' },
      { name: '/logs',   desc: 'Show backend output in the VS Code panel' },
    ];

    // Active slash command state
    let activeSlashCmd = null;   // e.g. '/fix'
    let suggestionIndex = -1;

    // ── Slash suggestion helpers ──────────────────────────────────
    function openSuggestions(query) {
      const matches = query === ''
        ? SLASH_COMMANDS
        : SLASH_COMMANDS.filter(c => c.name.startsWith('/' + query));
      if (matches.length === 0) { closeSuggestions(); return; }

      suggestionIndex = 0;
      slashSuggestionsEl.innerHTML = '';
      matches.forEach((cmd, i) => {
        const item = document.createElement('div');
        item.className = 'slash-suggestion-item' + (i === 0 ? ' active' : '');
        item.dataset.cmd = cmd.name;
        item.innerHTML =
          '<span class="slash-suggestion-name">' + escHtml(cmd.name) + '</span>' +
          '<span class="slash-suggestion-desc">' + escHtml(cmd.desc) + '</span>';
        item.addEventListener('mousedown', (e) => {
          e.preventDefault();
          selectSlashCommand(cmd.name);
        });
        slashSuggestionsEl.appendChild(item);
      });
      slashSuggestionsEl.classList.add('open');
    }

    function openModeSuggestions() {
      slashSuggestionsEl.innerHTML = '';
      const modes = ['junior', 'senior', 'expert'];
      suggestionIndex = modes.indexOf(currentMode);
      if (suggestionIndex === -1) suggestionIndex = 0;
      modes.forEach((mode, i) => {
        const m = MODE_META[mode];
        const item = document.createElement('div');
        item.className = 'slash-sub-item' + (i === suggestionIndex ? ' active' : '');
        item.dataset.mode = mode;
        item.innerHTML =
          '<span class="slash-sub-name ' + m.cls + '">' + m.icon + ' ' + escHtml(m.label) + '</span>' +
          '<span class="slash-sub-desc">' + escHtml(m.desc) + '</span>';
        item.addEventListener('mousedown', (e) => {
          e.preventDefault();
          selectMode(mode);
        });
        slashSuggestionsEl.appendChild(item);
      });
      slashSuggestionsEl.classList.add('open');
    }

    function selectMode(mode) {
      if (modeLocked) {
        pendingMode = mode;
        modeChangeTargetEl.textContent = MODE_META[mode]?.label ?? mode;
        modeChangeConfirmEl.style.display = 'flex';
        closeSuggestions();
        return;
      }
      applyMode(mode);
      // Clear the /mode token from the input
      const raw = inputEl.value;
      const slashMatch = raw.match(new RegExp('^([ \\t]*)[/][^ \\t]*'));
      inputEl.value = slashMatch ? raw.slice(slashMatch[0].length).trimStart() : raw;
      inputEl.style.height = 'auto';
      inputEl.style.height = Math.min(inputEl.scrollHeight, 160) + 'px';
      closeSuggestions();
      inputEl.focus();
    }

    function closeSuggestions() {
      slashSuggestionsEl.classList.remove('open');
      slashSuggestionsEl.innerHTML = '';
      suggestionIndex = -1;
    }

    function moveSuggestion(dir) {
      const items = slashSuggestionsEl.querySelectorAll('.slash-suggestion-item, .slash-sub-item');
      if (items.length === 0) return false;
      items[suggestionIndex]?.classList.remove('active');
      suggestionIndex = (suggestionIndex + dir + items.length) % items.length;
      items[suggestionIndex].classList.add('active');
      return true;
    }

    function selectActiveSuggestion() {
      const cmdItem = slashSuggestionsEl.querySelector('.slash-suggestion-item.active');
      if (cmdItem) { selectSlashCommand(cmdItem.dataset.cmd); return; }
      const modeItem = slashSuggestionsEl.querySelector('.slash-sub-item.active');
      if (modeItem) { selectMode(modeItem.dataset.mode); }
    }

    let _funMode = false;
    // Each entry: [w-j word, w-a word, w-m word, w-e word]
    const _jameWords = {
      normal: ['Just', 'A', 'Model-Driven', 'Engineer'],
      fun:    ['Just', 'Another', 'Mad', 'Engineer'],
    };
    function setJameTitle(fun) {
      const title = document.getElementById('jameTitle');
      if (!title) return;
      const words = fun ? _jameWords.fun : _jameWords.normal;
      title.innerHTML =
        '<span class="jame-letters"><span class="j">J</span><span class="a">A</span><span class="m">M</span><span class="e">E</span></span>' +
        '<span class="jame-acronym">' +
          '<span class="w-j">' + words[0] + '</span>' +
          '<span class="w-a">' + words[1] + '</span>' +
          '<span class="w-m">' + words[2] + '</span>' +
          '<span class="w-e">' + words[3] + '</span>' +
        '</span>';
    }

    function selectSlashCommand(name) {
      if (name === '/clear') { closeSuggestions(); inputEl.value = ''; doNewChat(); return; }
      if (name === '/logs') {
        const raw = inputEl.value;
        const slashMatch = raw.match(new RegExp('^([ \\t]*)[/][^ \\t]*'));
        inputEl.value = slashMatch ? raw.slice(slashMatch[0].length).trimStart() : raw;
        closeSuggestions();
        vscode.postMessage({ command: 'showLogs' });
        inputEl.focus();
        return;
      }
      if (name === '/fun') {
        _funMode = !_funMode;
        setJameTitle(_funMode);
        const raw = inputEl.value;
        const slashMatch = raw.match(new RegExp('^([ \\t]*)[/][^ \\t]*'));
        inputEl.value = slashMatch ? raw.slice(slashMatch[0].length).trimStart() : raw;
        closeSuggestions();
        addSysMsg(_funMode ? 'Just Another Mad Engineer — engaged. 😈' : 'Back to Just A Model-Driven Engineer.', 'info');
        saveState();
        inputEl.focus();
        return;
      }
      if (name === '/mode') {
        // Clear the /mode token and open mode sub-picker
        const raw = inputEl.value;
        const slashMatch = raw.match(new RegExp('^([ \\t]*)[/][^ \\t]*'));
        inputEl.value = slashMatch ? raw.slice(slashMatch[0].length).trimStart() : raw;
        closeSuggestions();
        openModeSuggestions();
        return;
      }
      // Regular slash command → becomes a pill prefix
      activeSlashCmd = name;
      slashPillLabel.textContent = name;
      slashPillEl.classList.add('visible');
      const raw = inputEl.value;
      const slashMatch = raw.match(new RegExp('^([ \\t]*)[/][^ \\t]*'));
      inputEl.value = slashMatch ? raw.slice(slashMatch[0].length) : raw;
      inputEl.style.height = 'auto';
      inputEl.style.height = Math.min(inputEl.scrollHeight, 160) + 'px';
      closeSuggestions();
      inputEl.focus();
    }

    function clearSlashCommand() {
      activeSlashCmd = null;
      slashPillEl.classList.remove('visible');
      slashPillLabel.textContent = '';
    }

    // Dismiss pill when X is clicked
    slashPillX.addEventListener('mousedown', (e) => {
      e.preventDefault();
      clearSlashCommand();
      inputEl.focus();
    });

    newChatConfirmOk.addEventListener('click', () => {
      newChatConfirmEl.style.display = 'none';
      doNewChat();
    });
    newChatConfirmCancel.addEventListener('click', () => {
      newChatConfirmEl.style.display = 'none';
    });

    let currentRunId     = null;
    let ws               = null;
    let isRunning        = false;
    let generatedFiles   = [];   // relative paths from file_generated
    let projectDir       = null;
    let currentIteration = 0;
    let lastAgent        = null;
    // files panel state
    let fpFiles          = {};   // path → lines
    let fpDecided        = new Set(); // paths already individually accepted/discarded

    // ── State persistence (survives panel moves) ──────────────────
    function saveState() {
      vscode.setState({
        feedHtml:    feed.innerHTML,
        fpFiles:     fpFiles,
        fpDecided:   [...fpDecided],
        fpVisible:   filesPanel.classList.contains('visible'),
        fpBodyHtml:  filesPanelBody.innerHTML,
        fpSummary:   filesPanelSummary.textContent,
        currentRunId: currentRunId,
        isRunning:   isRunning,
        currentMode: currentMode,
        modeLocked:  modeLocked,
        funMode:     _funMode,
      });
    }

    // Restore persisted state on load
    (function restoreState() {
      const s = vscode.getState();
      if (!s) return;
      if (s.feedHtml)   { feed.innerHTML = s.feedHtml; removeEmpty(); }
      if (s.fpFiles)    { fpFiles = s.fpFiles; }
      if (s.fpDecided)  { fpDecided = new Set(s.fpDecided); }
      if (s.fpBodyHtml) { filesPanelBody.innerHTML = s.fpBodyHtml; }
      if (s.fpSummary)  { filesPanelSummary.textContent = s.fpSummary; }
      if (s.fpVisible)  { filesPanel.classList.add('visible'); }
      if (s.currentRunId) { currentRunId = s.currentRunId; }
      if (s.currentMode) { applyMode(s.currentMode); }
      if (s.modeLocked)  { lockMode(); }
      if (s.funMode)     { _funMode = true; setJameTitle(true); }
      // Restore running state display (but don't re-enable input — run may be over)
      if (s.isRunning)  { setRunning(true); }
    })();

    // Keep All = accept all undecided diffs; Undo All = discard all undecided diffs
    fpKeepBtn.addEventListener('click', () => {
      const undecided = Object.keys(fpFiles).filter(p => !fpDecided.has(p));
      vscode.postMessage({ command: 'acceptAll', paths: undecided });
      // Remove all undecided rows from the panel
      undecided.forEach(p => {
        const row = filesPanelBody.querySelector('[data-path="' + CSS.escape(p) + '"]');
        if (row) row.remove();
        delete fpFiles[p];
        fpDecided.add(p);
      });
      updateFilesPanelSummary();
      if (Object.keys(fpFiles).length === 0) filesPanel.classList.remove('visible');
    });
    fpUndoBtn.addEventListener('click', () => {
      const undecided = Object.keys(fpFiles).filter(p => !fpDecided.has(p));
      vscode.postMessage({ command: 'discardAll', paths: undecided });
      // Remove all undecided rows from the panel
      undecided.forEach(p => {
        const row = filesPanelBody.querySelector('[data-path="' + CSS.escape(p) + '"]');
        if (row) row.remove();
        delete fpFiles[p];
        fpDecided.add(p);
      });
      updateFilesPanelSummary();
      if (Object.keys(fpFiles).length === 0) filesPanel.classList.remove('visible');
    });

    // ── Files panel helpers ───────────────────────────────────────
    function updateFilesPanelSummary() {
      const count = Object.keys(fpFiles).length;
      if (count === 0) {
        filesPanelSummary.textContent = '';
        saveState();
        return;
      }
      const totalLines = Object.values(fpFiles).reduce(function(a, b) { return a + b; }, 0);
      filesPanelSummary.textContent = count + ' file' + (count !== 1 ? 's' : '') + '  +' + totalLines;
      saveState();
    }

    function resetFilesPanel() {
      fpFiles = {};
      fpDecided = new Set();
      filesPanelBody.innerHTML = '';
      filesPanelSummary.textContent = '';
      fpKeepBtn.textContent = 'Keep All';
      fpKeepBtn.disabled = false;
      fpUndoBtn.textContent = 'Undo All';
      fpUndoBtn.disabled = false;
      filesPanel.classList.remove('visible');
      const v = filesPanel.querySelector('.fp-verdict');
      if (v) v.remove();
    }

    function addFileToPanel(relPath, content) {
      const lines = content ? content.split('\\n').length : 0;
      fpFiles[relPath] = lines;

      // Remove existing row for this path (revision update)
      const existing = filesPanelBody.querySelector('[data-path="' + CSS.escape(relPath) + '"]');
      if (existing) existing.remove();

      const ext = (relPath.split('.').pop() || 'file').toLowerCase();
      const name = relPath.split('/').pop() || relPath;

      const row = document.createElement('div');
      row.className = 'fp-row';
      row.dataset.path = relPath;
      row.innerHTML =
        '<span class="fp-ext">' + escHtml(ext) + '</span>' +
        '<span class="fp-name" title="' + escHtml(relPath) + '">' + escHtml(name) + '</span>' +
        '<span class="fp-lines">+' + lines + '</span>' +
        '<span class="fp-action fp-keep" title="Accept this file">✓</span>' +
        '<span class="fp-action fp-undo" title="Discard this file">✗</span>';

      // Click on filename → open diff
      row.querySelector('.fp-name').addEventListener('click', (e) => {
        e.stopPropagation();
        vscode.postMessage({ command: 'openProposedChange', filePath: relPath, fileContent: content });
      });
      // ✓ = accept this file's diff → remove from list
      row.querySelector('.fp-keep').addEventListener('click', (e) => {
        e.stopPropagation();
        vscode.postMessage({ command: 'acceptFile', filePath: relPath });
        fpDecided.add(relPath);
        delete fpFiles[relPath];
        row.remove();
        updateFilesPanelSummary();
        if (Object.keys(fpFiles).length === 0) filesPanel.classList.remove('visible');
      });
      // ✗ = discard this file's diff → remove from list
      row.querySelector('.fp-undo').addEventListener('click', (e) => {
        e.stopPropagation();
        vscode.postMessage({ command: 'discardFile', filePath: relPath });
        fpDecided.add(relPath);
        delete fpFiles[relPath];
        row.remove();
        updateFilesPanelSummary();
        if (Object.keys(fpFiles).length === 0) filesPanel.classList.remove('visible');
      });

      filesPanelBody.appendChild(row);

      updateFilesPanelSummary();
      filesPanel.classList.add('visible');
    }

    function setFilesPanelVerdict(qaPassed) {
      let v = filesPanel.querySelector('.fp-verdict');
      if (!v) {
        v = document.createElement('div');
        filesPanel.appendChild(v);
      }
      v.className = 'fp-verdict ' + (qaPassed ? 'pass' : 'fail');
      v.textContent = qaPassed ? '✓ QA passed' : '✗ QA did not pass';
    }

    // ── Progress helpers ─────────────────────────────────────────
    function setProgressIndeterminate() {
      progressBar.classList.add('indeterminate');
      progressBar.style.width = '';
    }
    function clearProgress() {
      progressBar.classList.remove('indeterminate');
      progressBar.style.width = '0%';
    }

    // ── Prompt history ────────────────────────────────────────────
    let promptHistory    = [];   // array of sent prompt strings
    let historyIndex     = -1;   // -1 = not browsing history
    let historyDraft     = '';   // saved draft while browsing

    // ── Auto-resize textarea + slash detection ────────────────────
    inputEl.addEventListener('input', () => {
      inputEl.style.height = 'auto';
      inputEl.style.height = Math.min(inputEl.scrollHeight, 160) + 'px';
      // Reset history browsing if user edits manually
      if (historyIndex !== -1) {
        historyIndex = -1;
        historyDraft = '';
      }
      // Slash suggestion: only trigger when no pill is active and input
      // starts with optional whitespace + '/' (nothing else before it)
      if (!activeSlashCmd) {
        const val = inputEl.value;
        const slashMatch = val.match(new RegExp('^[ \\t]*[/]([a-zA-Z0-9_-]*)$'));
        if (slashMatch) {
          openSuggestions(slashMatch[1]);
        } else {
          closeSuggestions();
        }
      }
    });

    // ── Keyboard ─────────────────────────────────────────────────
    inputEl.addEventListener('keydown', (e) => {
      // Suggestion navigation
      if (slashSuggestionsEl.classList.contains('open')) {
        if (e.key === 'ArrowDown') { e.preventDefault(); moveSuggestion(1); return; }
        if (e.key === 'ArrowUp')   { e.preventDefault(); moveSuggestion(-1); return; }
        if (e.key === 'Escape')    { e.preventDefault(); closeSuggestions(); return; }
        if (e.key === 'Enter' && !e.shiftKey) {
          e.preventDefault();
          selectActiveSuggestion();
          return;
        }
        if (e.key === 'Tab') {
          e.preventDefault();
          selectActiveSuggestion();
          return;
        }
      }

      // Enter → send (Shift+Enter inserts newline naturally via default)
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        send();
        return;
      }

      // Backspace at empty input with active pill → remove pill
      if (e.key === 'Backspace' && activeSlashCmd && inputEl.value === '') {
        clearSlashCommand();
        return;
      }

      // Up arrow — navigate to older prompt (only when caret is on first line)
      if (e.key === 'ArrowUp') {
        const selStart = inputEl.selectionStart;
        const beforeCaret = inputEl.value.substring(0, selStart);
        const onFirstLine = !beforeCaret.includes('\\n');
        if (onFirstLine && promptHistory.length > 0) {
          e.preventDefault();
          if (historyIndex === -1) {
            historyDraft = inputEl.value;
            historyIndex = promptHistory.length - 1;
          } else if (historyIndex > 0) {
            historyIndex--;
          }
          inputEl.value = promptHistory[historyIndex];
          inputEl.style.height = 'auto';
          inputEl.style.height = Math.min(inputEl.scrollHeight, 160) + 'px';
          inputEl.setSelectionRange(0, 0);
        }
        return;
      }

      // Down arrow — navigate to newer prompt or back to draft
      if (e.key === 'ArrowDown') {
        if (historyIndex === -1) return;
        const selStart = inputEl.selectionStart;
        const afterCaret = inputEl.value.substring(selStart);
        const onLastLine = !afterCaret.includes('\\n');
        if (onLastLine) {
          e.preventDefault();
          if (historyIndex < promptHistory.length - 1) {
            historyIndex++;
            inputEl.value = promptHistory[historyIndex];
          } else {
            historyIndex = -1;
            inputEl.value = historyDraft;
            historyDraft = '';
          }
          inputEl.style.height = 'auto';
          inputEl.style.height = Math.min(inputEl.scrollHeight, 160) + 'px';
          const len = inputEl.value.length;
          inputEl.setSelectionRange(len, len);
        }
        return;
      }
    });

    sendBtn.addEventListener('click', send);
    stopBtn.addEventListener('click', stop);
    showLogsBtn.addEventListener('click', () => vscode.postMessage({ command: 'showLogs' }));
    newChatBtn.addEventListener('click', newChat);

    // ── Running state ─────────────────────────────────────────────
    function setRunning(running) {
      isRunning = running;
      inputEl.disabled = running;
      sendBtn.style.display = running ? 'none' : 'flex';
      stopBtn.style.display = running ? 'flex' : 'none';
      if (running) setProgressIndeterminate();
      else clearProgress();
      saveState();
    }

    // ── Utilities ─────────────────────────────────────────────────
    function escHtml(s) {
      return String(s)
        .replace(new RegExp('&', 'g'), '&amp;')
        .replace(new RegExp('<', 'g'), '&lt;')
        .replace(new RegExp('>', 'g'), '&gt;')
        .replace(new RegExp('"', 'g'), '&quot;');
    }

    // Lightweight markdown renderer (no external lib — webview is sandboxed)
    function renderMd(text) {
      if (!text) return '';
      let s = escHtml(String(text));
      // Bold: use [*][*] so * is inside a character class and not a quantifier
      s = s.replace(new RegExp('[*][*](.+?)[*][*]', 'g'), '<strong>$1</strong>');
      // List items
      s = s.replace(new RegExp('^- (.+)$', 'gm'), '<li style="margin-left:12px;list-style:disc">$1</li>');
      // Newlines
      s = s.replace(new RegExp('[\\n]', 'g'), '<br>');
      return s;
    }

    function removeEmpty() { const e = document.getElementById('emptyState'); if (e) e.remove(); }
    function scrollFeed()  { feed.scrollTop = feed.scrollHeight; saveState(); }

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
    const _sysIcons = { ok: '✓', warn: '⚠', err: '✕', info: '›' };
    function addSysMsg(text, type = 'info') {
      const icon = _sysIcons[type] || _sysIcons.info;
      const safe = text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
      const el = document.createElement('div');
      el.className = 'sys-msg ' + type + ' fade-in';
      el.innerHTML = '<i class="sys-msg-icon">' + icon + '</i><span class="sys-msg-text">' + safe + '</span>';
      removeEmpty();
      feed.appendChild(el);
      scrollFeed();
      saveState();
    }

    // ── Agent log rows ────────────────────────────────────────────
    // Short display labels (keyed by lowercase)
    const AGENT_SHORT = {
      'architect':          'ARCH',
      'developer':          'DEV',
      'delivery':           'DEV',
      'delivery_engineer':  'DEV',
      'quality_engineer':   'QA',
      'qa':                 'QA',
      'devops':             'OPS',
      'exercise_generator': 'EXC',
      'exercise':           'EXC',
      'orchestrator':       'SYS',
      'system':             'SYS',
    };

    function acClass(agentName) {
      const n = (agentName || '').toLowerCase();
      if (n.includes('architect'))                                                    return 'ac-architect';
      if (n.includes('developer') || n.includes('devop') || n.includes('delivery'))  return 'ac-developer';
      if (n.includes('quality') || n.includes('qa'))                                 return 'ac-qa';
      return 'ac-system';
    }

    function phaseClass(phase) {
      if (!phase) return '';
      // Take only the first segment of compound phases like "CONSTRUCTION/build-and-test"
      const p = String(phase).toLowerCase().split('/')[0].replace(new RegExp('[^a-z_]', 'g'), '_');
      const known = ['plan','act','reason','design','validate','self_critique',
                     'classify','interrogation','construction','learning'];
      return 'phase-' + (known.indexOf(p) >= 0 ? p : 'other');
    }

    // Canonical short labels for phase pills
    const PHASE_LABEL = {
      'plan':          'PLAN',
      'act':           'ACT',
      'reason':        'REASON',
      'design':        'DESIGN',
      'validate':      'VALIDATE',
      'self_critique': 'CRITIQUE',
      'classify':      'CLASSIFY',
      'interrogation': 'INTERROGATE',
      'construction':  'BUILD',
      'learning':      'LEARN',
    };

    // Short display label for a phase (shown in pill)
    function phaseLabel(phase) {
      if (!phase) return '';
      const key = String(phase).toLowerCase().split('/')[0].replace(new RegExp('[^a-z_]', 'g'), '_');
      if (PHASE_LABEL[key]) return PHASE_LABEL[key];
      // Compound phase sub-label: "CONSTRUCTION/build-and-test" → "BUILD AND TEST"
      const parts = String(phase).toUpperCase().split('/');
      if (parts.length > 1) return parts[1].replace(new RegExp('-', 'g'), ' ');
      return parts[0];
    }

    // Track the last agent class appended so we can dim repeated tags
    let _lastRowAc = null;

    function addAgentRow(agentName, text, thinking, phase) {
      removeEmpty();
      const nameLower = (agentName || '').toLowerCase();
      const rawKey = nameLower.replace(new RegExp('\\s+', 'g'), '_');
      const ac = acClass(agentName);
      const shortTag = AGENT_SHORT[nameLower] || AGENT_SHORT[rawKey] || (agentName || 'SYS').slice(0, 4).toUpperCase();
      const sameAgent = (ac === _lastRowAc);
      _lastRowAc = ac;

      // ── Row ──────────────────────────────────────────────────────
      const row = document.createElement('div');
      row.className = 'agent-row ' + ac + (sameAgent ? ' same-agent' : '') + ' fade-in';

      // Agent tag (fixed-width column)
      const tag = document.createElement('span');
      tag.className = 'ar-tag';
      tag.textContent = shortTag;
      row.appendChild(tag);

      // Phase pill (optional)
      if (phase) {
        const pill = document.createElement('span');
        pill.className = 'ar-phase ' + phaseClass(phase);
        pill.textContent = phaseLabel(phase);
        row.appendChild(pill);
      }

      // Message text
      const msgEl = document.createElement('span');
      msgEl.className = 'ar-msg';
      msgEl.innerHTML = renderMd(text);
      row.appendChild(msgEl);

      // Thinking toggle button (only if thinking exists)
      let thinkContent = null;
      if (thinking) {
        const btn = document.createElement('span');
        btn.className = 'ar-think-btn';
        btn.textContent = '▶ thinking';
        row.appendChild(btn);

        thinkContent = document.createElement('div');
        thinkContent.className = 'ar-think-content';
        thinkContent.textContent = thinking;

        btn.addEventListener('click', (e) => {
          e.stopPropagation();
          thinkContent.classList.toggle('shown');
          btn.textContent = thinkContent.classList.contains('shown') ? '▼ thinking' : '▶ thinking';
        });
      }

      feed.appendChild(row);
      if (thinkContent) feed.appendChild(thinkContent);

      scrollFeed();
    }

    // Shims for any remaining calls using old names
    function addAgentCard(agentName, text, thinking, phase) {
      addAgentRow(agentName, text, thinking, phase);
    }
    function addTlRow(agentName, text, thinking, phase) {
      addAgentRow(agentName, text, thinking, phase);
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

      // Accept All proposed changes
      const acceptAllBtn = document.createElement('button');
      acceptAllBtn.className = 'btn btn-success';
      acceptAllBtn.textContent = 'Accept All';
      acceptAllBtn.title = 'Write all proposed files to workspace';
      acceptAllBtn.addEventListener('click', () => {
        vscode.postMessage({ command: 'acceptAll' });
        acceptAllBtn.textContent = 'Accepted';
        acceptAllBtn.disabled = true;
        discardAllBtn.disabled = true;
      });
      row.appendChild(acceptAllBtn);

      // Discard All proposed changes
      const discardAllBtn = document.createElement('button');
      discardAllBtn.className = 'btn btn-danger';
      discardAllBtn.textContent = 'Discard All';
      discardAllBtn.title = 'Discard all proposed changes';
      discardAllBtn.addEventListener('click', () => {
        vscode.postMessage({ command: 'discardAll' });
        discardAllBtn.textContent = 'Discarded';
        discardAllBtn.disabled = true;
        acceptAllBtn.disabled = true;
      });
      row.appendChild(discardAllBtn);

      // Save to workspace (existing absolute paths fallback)
      if (files.length > 0) {
        const saveBtn = document.createElement('button');
        saveBtn.className = 'btn btn-secondary';
        saveBtn.textContent = 'Save paths';
        saveBtn.title = 'Copy generated files to workspace using absolute paths';
        saveBtn.addEventListener('click', () => {
          vscode.postMessage({ command: 'saveFiles', files, projectDir: pDir });
          saveBtn.textContent = 'Saved';
          saveBtn.disabled = true;
        });
        row.appendChild(saveBtn);

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

    // ── Clarification card ────────────────────────────────────────
    const CLARIFY_PAGE_SIZE = 4;

    function showClarificationCard(question, options) {
      const opts = options && options.length > 0 ? options : [];
      let page = 0;
      let selectedOpt = null; // index into opts, or null
      const totalPages = opts.length > 0 ? Math.ceil(opts.length / CLARIFY_PAGE_SIZE) : 0;

      const card = document.createElement('div');
      card.className = 'clarify-card fade-in';

      // Question header
      const qEl = document.createElement('div');
      qEl.className = 'clarify-q';
      qEl.textContent = question;
      card.appendChild(qEl);

      // Options list
      const optsEl = document.createElement('div');
      optsEl.className = 'clarify-opts';
      card.appendChild(optsEl);

      function renderPage() {
        optsEl.innerHTML = '';
        const start = page * CLARIFY_PAGE_SIZE;
        const slice = opts.slice(start, start + CLARIFY_PAGE_SIZE);
        slice.forEach((opt, i) => {
          const globalIdx = start + i;
          // Support "label|desc" or plain string
          const parts = opt.split('|');
          const label = parts[0].trim();
          const desc  = parts[1] ? parts[1].trim() : '';

          const row = document.createElement('div');
          row.className = 'clarify-opt' + (selectedOpt === globalIdx ? ' selected' : '');
          row.innerHTML =
            '<span class="clarify-opt-num">' + (globalIdx + 1) + '</span>' +
            '<span class="clarify-opt-body">' +
              '<span class="clarify-opt-label">' + escHtml(label) + '</span>' +
              (desc ? '<span class="clarify-opt-desc">' + escHtml(desc) + '</span>' : '') +
            '</span>' +
            '<span class="clarify-opt-check">&#10003;</span>';
          row.addEventListener('click', () => {
            selectedOpt = globalIdx;
            submitAnswer(label);
          });
          optsEl.appendChild(row);
        });
      }

      // Free-text row
      const freeRow = document.createElement('div');
      freeRow.className = 'clarify-free-row';
      const textArea = document.createElement('textarea');
      textArea.className = 'clarify-text';
      textArea.placeholder = opts.length ? 'Or type your own answer…' : 'Type your answer…';
      textArea.rows = 1;
      const sendBtn = document.createElement('button');
      sendBtn.className = 'btn btn-primary';
      sendBtn.style.cssText = 'padding:4px 10px;font-size:11px;align-self:flex-end';
      sendBtn.textContent = 'Send';
      sendBtn.addEventListener('click', () => submitAnswer(textArea.value));
      textArea.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submitAnswer(textArea.value); }
      });
      freeRow.appendChild(textArea);
      freeRow.appendChild(sendBtn);
      card.appendChild(freeRow);

      // Pagination footer (only if multiple pages)
      let prevBtn, nextBtn, pageLabel;
      if (totalPages > 1) {
        const footer = document.createElement('div');
        footer.className = 'clarify-footer';
        prevBtn = document.createElement('button');
        prevBtn.className = 'clarify-nav';
        prevBtn.innerHTML = '&#8249;';
        prevBtn.addEventListener('click', () => { if (page > 0) { page--; renderPage(); updateNav(); } });
        nextBtn = document.createElement('button');
        nextBtn.className = 'clarify-nav';
        nextBtn.innerHTML = '&#8250;';
        nextBtn.addEventListener('click', () => { if (page < totalPages - 1) { page++; renderPage(); updateNav(); } });
        pageLabel = document.createElement('span');
        pageLabel.className = 'clarify-page';
        footer.appendChild(prevBtn);
        footer.appendChild(nextBtn);
        footer.appendChild(pageLabel);
        card.appendChild(footer);
      }

      function updateNav() {
        if (!prevBtn) return;
        prevBtn.disabled = page === 0;
        nextBtn.disabled = page === totalPages - 1;
        pageLabel.textContent = (page + 1) + '/' + totalPages;
      }

      function submitAnswer(answer) {
        if (!answer.trim()) return;
        const ans = answer.trim();
        // Replace card with compact Q/A summary
        const summary = document.createElement('div');
        summary.className = 'clarify-summary fade-in';
        summary.innerHTML =
          '<span class="cs-q">Q: ' + escHtml(question) + '</span><br>' +
          '<span class="cs-a">A: ' + escHtml(ans) + '</span>';
        card.replaceWith(summary);
        vscode.postMessage({ command: 'submitClarification', runId: currentRunId, answer: ans });
        saveState();
      }

      if (opts.length > 0) renderPage();
      else optsEl.remove();
      updateNav();

      feed.appendChild(card);
      scrollFeed();
      saveState();
    }

    // ── Send / stop ───────────────────────────────────────────────
    function send() {
      const text = inputEl.value.trim();
      const slashCmd = activeSlashCmd;
      // Allow send with just a slash command and no body text
      if (!text && !slashCmd || isRunning) return;

      const mode = currentMode;

      // Build display text: pill label + body
      const displayText = slashCmd ? slashCmd + (text ? ' ' + text : '') : text;

      // Save to prompt history (avoid duplicating the last entry)
      if (promptHistory.length === 0 || promptHistory[promptHistory.length - 1] !== displayText) {
        promptHistory.push(displayText);
      }
      historyIndex = -1;
      historyDraft = '';

      // Reset state
      generatedFiles = [];
      projectDir = null;
      currentIteration = 0;
      lastAgent = null;
      _lastRowAc = null;
      resetFilesPanel();

      addUserMessage(displayText);
      inputEl.value = '';
      inputEl.style.height = 'auto';
      clearSlashCommand();
      closeSuggestions();
      lockMode();
      setRunning(true);

      vscode.postMessage({ command: 'startRun', userRequest: displayText, mode, slashCommand: slashCmd });
    }

    function stop() {
      if (!currentRunId) return;
      vscode.postMessage({ command: 'cancelRun', runId: currentRunId });
      addSysMsg('Cancellation requested...', 'warn');
    }

    function newChat() {
      if (isRunning && currentRunId) {
        newChatConfirmEl.style.display = 'flex';
        return;
      }
      doNewChat();
    }

    function doNewChat() {
      // Cancel any in-flight run first
      if (isRunning && currentRunId) {
        vscode.postMessage({ command: 'cancelRun', runId: currentRunId });
      }

      // Reset all runtime state
      currentRunId = null;
      generatedFiles = [];
      projectDir = null;
      currentIteration = 0;
      lastAgent = null;
      _lastRowAc = null;
      promptHistory = [];
      historyIndex = -1;
      historyDraft = '';
      clearSlashCommand();
      closeSuggestions();
      unlockMode();
      setJameTitle(_funMode);
      if (ws) { ws.close(); ws = null; }

      // Reset UI
      setRunning(false);
      clearProgress();
      resetFilesPanel();
      inputEl.value = '';
      inputEl.style.height = 'auto';

      // Clear feed and restore empty state
      feed.innerHTML = '';
      const emptyDiv = document.createElement('div');
      emptyDiv.className = 'empty';
      emptyDiv.id = 'emptyState';
      emptyDiv.innerHTML =
        '<svg class="empty-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">' +
        '<path stroke-linecap="round" stroke-linejoin="round" d="M9.75 3.104v5.714a2.25 2.25 0 0 1-.659 1.591L5 14.5M9.75 3.104c-.251.023-.501.05-.75.082m.75-.082a24.301 24.301 0 0 1 4.5 0m0 0v5.714c0 .597.237 1.17.659 1.591L19.8 15.3M14.25 3.104c.251.023.501.05.75.082M19.8 15.3l-1.57.393A9.065 9.065 0 0 1 12 15a9.065 9.065 0 0 0-6.23-.693L5 14.5m14.8.8 1.402 1.402c1.232 1.232.65 3.318-1.067 3.611A48.309 48.309 0 0 1 12 21a48.25 48.25 0 0 1-8.135-.687c-1.718-.293-2.3-2.379-1.067-3.61L5 14.5" /></svg>' +
        '<p>Describe what you want to build. The multi-agent pipeline will architect, code, and validate it.</p>';
      feed.appendChild(emptyDiv);

      // Wipe persisted state so it doesn't restore on panel move
      vscode.setState(null);

      inputEl.focus();
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

      // When auto-start finds a free port, the resolved URL may differ from
      // the configured one — store it so the next runCreated uses the right WS URL.
      if (msg.command === 'backendUrlResolved') {
        window._resolvedBackendUrl = msg.backendUrl;
        return;
      }

      if (msg.command === 'runCreated') {
        currentRunId = msg.runId;
        addSysMsg('Run started [' + msg.runId.substring(0, 8) + ']', 'info');

        // Use the resolved URL (in case auto-start picked a different port)
        const effectiveUrl = window._resolvedBackendUrl || msg.backendUrl;
        const wsUrl = effectiveUrl
          .replace('http://', 'ws://')
          .replace('https://', 'wss://') + '/ws/runs/' + msg.runId;

        ws = new WebSocket(wsUrl);

        ws.onmessage = (evt) => {
          let data;
          try {
            data = JSON.parse(evt.data);
          } catch (e) {
            console.warn('[JAME] bad WS frame:', evt.data, e);
            return; // skip unparseable frame, keep listening
          }
          try {
            handleServerEvent(data);
          } catch (e) {
            console.error('[JAME] handleServerEvent threw:', e, data);
          }
        };

        ws.onclose = () => {
          // Always stop — covers cases where terminal event was missed
          setRunning(false);
        };

        ws.onerror = (e) => {
          console.error('[JAME] WebSocket error', e);
          addSysMsg('WebSocket error. Is the backend running?', 'err');
          setRunning(false);
        };
      }

      if (msg.command === 'filesSaved') {
        addSysMsg('Files saved to ' + msg.destDir, 'ok');
        return;
      }

      if (msg.command === 'allAccepted') {
        addSysMsg('All proposed changes accepted to workspace.', 'ok');
        return;
      }

      if (msg.command === 'allDiscarded') {
        addSysMsg('All proposed changes discarded.', 'warn');
        return;
      }
    });

    const AGENT_DISPLAY = {
      'architect':          'Architect',
      'developer':          'Developer',
      'delivery_engineer':  'Delivery',
      'quality_engineer':   'Quality Engineer',
      'devops':             'DevOps',
      'exercise_generator': 'Exercise',
    };

    function handleServerEvent(data) {
      if (!data || typeof data !== 'object') return;
      const event = String(data.event || '');
      const rawAgent = String(data.agent || '');
      const agentDisplay = AGENT_DISPLAY[rawAgent] || rawAgent || 'System';

      console.log('[JAME event]', event, rawAgent, data.message);

      // Mirror all events to the VS Code output channel so users can see them
      // even when the backend is externally managed (not spawned by the extension).
      {
        const ts = new Date().toISOString().substring(11, 23);
        const phase = data.phase ? `[${String(data.phase).toUpperCase()}]` : '';
        const agent = rawAgent ? `[${rawAgent.toUpperCase()}]` : '';
        vscode.postMessage({ command: 'logEvent', line: `${ts} ${agent}${phase} ${data.message || event}` });
      }

      if (event === 'run_started') {
        vscode.postMessage({ command: 'clearLogs' });
        vscode.postMessage({ command: 'logEvent', line: '─'.repeat(60) });
        vscode.postMessage({ command: 'logEvent', line: `[JAME] Run started at ${new Date().toISOString()}` });
        vscode.postMessage({ command: 'logEvent', line: '─'.repeat(60) });
        addSysMsg('Orchestration started', 'info');
        return;
      }

      if (event === 'agent_update') {
        const msg = String(data.message || '').trim();
        const payload = (data.payload && typeof data.payload === 'object') ? data.payload : {};
        const thinking = String(payload.thinking || '');
        // phase: prefer top-level field (set by service._emit), fall back to payload
        const phase = data.phase || payload.phase || null;

        // Skip truly empty messages
        if (!msg) return;

        // Only filter the exact "Generated N file(s)." pattern (not chunked/self-val messages)
        // These come from the developer "act" log and duplicate file_generated events.
        if (
          (rawAgent === 'developer') &&
          new RegExp('^Generated \\d+ file\\(s\\)', 'i').test(msg)
        ) {
          return;
        }

        // Detect QA → Developer transition → insert iteration separator
        if (
          lastAgent &&
          lastAgent !== agentDisplay &&
          (lastAgent === 'Quality Engineer' || lastAgent === 'QA' || lastAgent === 'quality_engineer') &&
          (agentDisplay === 'Developer' || rawAgent === 'developer')
        ) {
          currentIteration++;
          _lastRowAc = null;
          addIterSep('QA feedback — revision ' + currentIteration);
        }

        lastAgent = agentDisplay;
        addTlRow(agentDisplay, msg, thinking, phase);
        return;
      }

      if (event === 'file_generated') {
        const p = data.payload;
        if (p && p.path) {
          // Save to workspace immediately so diff editor has real content
          vscode.postMessage({ command: 'openProposedChange', filePath: p.path, fileContent: p.content || '' });
          addFileToPanel(p.path, p.content || '');
          generatedFiles.push(p.path);
        }
        return;
      }

      if (event === 'files_ready') {
        // files_ready just confirms all files are on disk — panel already shows them
        return;
      }

      if (event === 'clarification_request') {
        const p = data.payload || {};
        const question = p.question || data.message || 'Please clarify:';
        const options = p.options || [];
        showClarificationCard(question, options);
        return;
      }

      if (event === 'run_completed') {
        const p = data.payload || {};
        projectDir = p.project_dir || null;
        const qaPassed = !!p.qa_passed;
        setFilesPanelVerdict(qaPassed);
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

      // Unknown event — log to console, don't silently drop
      console.warn('[JAME] unrecognized event:', event, data);
    }

    // Focus on load
    inputEl.focus();
  </script>
</body>
</html>`;
  }
}
