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
  /** Backend instance ID from /health — changes on every restart. */
  private knownInstanceId?: string;
  /** Proposed file contents keyed by relative path, for inline diff editor. */
  private proposedFiles: Map<string, string> = new Map();
  /** Generated file contents keyed by relative path, from the most recent run. */
  private generatedFiles: Map<string, string> = new Map();
  /** Output channel that surfaces backend stdout+stderr logs. */
  private outputChannel: vscode.OutputChannel = vscode.window.createOutputChannel("JAME Backend");

  constructor(private readonly extensionUri: vscode.Uri) {}

  /** Returns proposed content for the jame-proposed:// URI scheme. */
  public getProposedContent(filePath: string): string {
    return this.proposedFiles.get(filePath) ?? "";
  }

  /** Returns the in-memory proposed files map (live reference). */
  public getProposedFiles(): Map<string, string> {
    return this.proposedFiles;
  }

  /** Returns the in-memory generated files map (live reference). */
  public getGeneratedFiles(): Map<string, string> {
    return this.generatedFiles;
  }

  /** Open a VS Code diff editor between workspace file and proposed content. */
  private async openProposedChange(filePath: string, content: string): Promise<void> {
    this.proposedFiles.set(filePath, content);
    // Also record in generatedFiles so fileReader can find it even without the diff editor open
    this.generatedFiles.set(filePath, content);

    const workspaceRoot = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
    if (!workspaceRoot) {
      // No workspace — just store in memory, nothing to write
      return;
    }

    const destPath = path.join(workspaceRoot, filePath);

    // Capture the previous content (if any) for the diff "original" side
    const hadExisting = fs.existsSync(destPath);
    const previousContent = hadExisting ? fs.readFileSync(destPath, "utf8") : null;

    // Write immediately so the file is on disk before the diff editor opens.
    fs.mkdirSync(path.dirname(destPath), { recursive: true });
    fs.writeFileSync(destPath, content, "utf8");

    // Open the diff editor asynchronously (fire-and-forget) so streaming of
    // subsequent file_generated events is not blocked waiting for the editor.
    const label = `${path.basename(filePath)} (JAME generated)`;
    if (hadExisting && previousContent !== null) {
      this.proposedFiles.set(`__prev__${filePath}`, previousContent);
      vscode.commands.executeCommand(
        "vscode.diff",
        vscode.Uri.parse(`jame-proposed:__prev__${filePath}`),
        vscode.Uri.file(destPath),
        label
      ).then(undefined, () => {/* ignore if tab can't open */});
    } else {
      vscode.commands.executeCommand(
        "vscode.diff",
        vscode.Uri.parse("jame-proposed:__empty__"),
        vscode.Uri.file(destPath),
        label
      ).then(undefined, () => {/* ignore if tab can't open */});
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
        line?: string;
        toolCallId?: string;
        action?: string;
        feedback?: string;
        prompt?: string;
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

      if (msg.command === "openExerciseFile") {
        // Write the stub to disk and open it for editing (no diff — junior edits the stub directly)
        const workspaceRoot = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
        if (workspaceRoot && msg.filePath && msg.fileContent !== undefined) {
          const destPath = path.join(workspaceRoot, msg.filePath);
          fs.mkdirSync(path.dirname(destPath), { recursive: true });
          if (!fs.existsSync(destPath)) {
            // Only write on first open — don't overwrite edits the junior already made
            fs.writeFileSync(destPath, msg.fileContent, "utf8");
          }
          this.generatedFiles.set(msg.filePath, msg.fileContent);
          const doc = await vscode.workspace.openTextDocument(vscode.Uri.file(destPath));
          await vscode.window.showTextDocument(doc, { preview: false });
        }
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

      if (msg.command === "submitSpecsReview") {
        const backendUrlForSpecs = this.resolvedBackendUrl ?? vscode.workspace
          .getConfiguration()
          .get<string>("jameWorkflow.backendUrl", "http://localhost:8000");
        const fetchFnSpecs = (globalThis as { fetch?: (input: string, init?: unknown) => Promise<any> }).fetch;
        if (fetchFnSpecs && msg.runId && msg.action) {
          try {
            await fetchFnSpecs(`${backendUrlForSpecs}/runs/${msg.runId}/approve-specs`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ action: msg.action, feedback: msg.feedback ?? "" }),
            });
          } catch { /* ignore */ }
        }
        return;
      }

      if (msg.command === "queuePrompt") {
        const backendUrlForQueue = this.resolvedBackendUrl ?? vscode.workspace
          .getConfiguration()
          .get<string>("jameWorkflow.backendUrl", "http://localhost:8000");
        const fetchFnQueue = (globalThis as { fetch?: (input: string, init?: unknown) => Promise<any> }).fetch;
        if (fetchFnQueue && msg.runId && msg.prompt) {
          try {
            await fetchFnQueue(`${backendUrlForQueue}/runs/${msg.runId}/queue-prompt`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ prompt: msg.prompt }),
            });
          } catch { /* ignore */ }
        }
        return;
      }

      if (msg.command === "submitExercise") {
        const backendUrlForSubmit = this.resolvedBackendUrl ?? vscode.workspace
          .getConfiguration()
          .get<string>("jameWorkflow.backendUrl", "http://localhost:8000");
        const fetchFnSubmit = (globalThis as { fetch?: (input: string, init?: unknown) => Promise<any> }).fetch;
        if (fetchFnSubmit && msg.runId) {
          const workspaceRoot = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
          const files: { path: string; content: string; language: string }[] = [];
          if (workspaceRoot) {
            // Read all generated/exercise files that have been saved to workspace
            for (const [relPath, _content] of this.generatedFiles.entries()) {
              const absPath = path.join(workspaceRoot, relPath);
              if (fs.existsSync(absPath)) {
                const content = fs.readFileSync(absPath, "utf-8");
                const ext = path.extname(relPath).replace(".", "") || "text";
                files.push({ path: relPath, content, language: ext });
              }
            }
          }
          try {
            const resp = await fetchFnSubmit(`${backendUrlForSubmit}/runs/${msg.runId}/submit`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ files }),
            });
            if (resp.ok) {
              const result = await resp.json() as { passed: boolean; score: number; feedback: string };
              webviewView.webview.postMessage({ command: "submitResult", passed: result.passed, score: result.score, feedback: result.feedback });
            }
          } catch { /* ignore */ }
        }
        return;
      }

      if (msg.command === "submitToolResponse") {
        const backendUrlForTool = this.resolvedBackendUrl ?? vscode.workspace
          .getConfiguration()
          .get<string>("jameWorkflow.backendUrl", "http://localhost:8000");
        const fetchFn3 = (globalThis as { fetch?: (input: string, init?: unknown) => Promise<any> }).fetch;
        if (fetchFn3 && msg.runId && msg.toolCallId && msg.action) {
          try {
            await fetchFn3(`${backendUrlForTool}/runs/${msg.runId}/tool-response`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ tool_call_id: msg.toolCallId, action: msg.action }),
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
      // Fetch instance_id to detect backend restarts
      const instanceId = await this.fetchInstanceId(backendUrl);
      const isNewInstance = instanceId && instanceId !== this.knownInstanceId;
      if (instanceId) {
        this.knownInstanceId = instanceId;
      }
      if (!this.resolvedBackendUrl) {
        this.resolvedBackendUrl = backendUrl;
        this.view?.webview.postMessage({ command: "backendUrlResolved", backendUrl });
        this.outputChannel.clear();
        this.outputChannel.appendLine(`[JAME] Backend already running at ${backendUrl} (externally managed).`);
        this.outputChannel.appendLine(`[JAME] Logs are in the terminal where you started the backend.`);
        this.outputChannel.appendLine(`[JAME] To see logs here, let the extension manage the backend (stop your manual process).`);
      }
      // Backend restarted (new instance) — clear the webview chat
      if (isNewInstance) {
        this.view?.webview.postMessage({ command: "clearChat" });
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
        // Capture instance_id on first healthy response after our own launch
        const instanceId = await this.fetchInstanceId(backendUrl);
        if (instanceId) { this.knownInstanceId = instanceId; }
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

  private async fetchInstanceId(backendUrl: string): Promise<string | undefined> {
    const fetchFn = (globalThis as { fetch?: (input: string, init?: unknown) => Promise<any> }).fetch;
    if (!fetchFn) { return undefined; }
    try {
      const resp = await fetchFn(`${backendUrl}/health`);
      if (!resp.ok) { return undefined; }
      const body = await resp.json();
      return body?.instance_id as string | undefined;
    } catch {
      return undefined;
    }
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
    const webview = this.view!.webview;
    const webviewDir = vscode.Uri.joinPath(this.extensionUri, "src", "webview");
    const cssUri = webview.asWebviewUri(vscode.Uri.joinPath(webviewDir, "view.css"));
    const jsUri  = webview.asWebviewUri(vscode.Uri.joinPath(webviewDir, "view.js"));
    const nonce  = require("crypto").randomUUID() as string;

    const templatePath = path.join(this.extensionUri.fsPath, "src", "webview", "view.html");
    const template = fs.readFileSync(templatePath, "utf8");

    const csp = [
      "default-src 'none'",
      `style-src ${webview.cspSource}`,
      `script-src 'nonce-${nonce}'`,
      "connect-src http://localhost:* ws://localhost:*",
    ].join("; ");

    return template
      .replace("{{CSP}}", csp)
      .replace("{{CSS_URI}}", cssUri.toString())
      .replace("{{JS_URI}}", jsUri.toString())
      .replace(/\{\{NONCE\}\}/g, nonce);
  }

}
