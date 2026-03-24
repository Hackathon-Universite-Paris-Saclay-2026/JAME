import * as vscode from "vscode";

type StartRunRequest = {
  userRequest: string;
};

export class JamePanel {
  public static currentPanel: JamePanel | undefined;

  private readonly panel: vscode.WebviewPanel;
  private readonly disposables: vscode.Disposable[] = [];

  public static render(extensionUri: vscode.Uri): void {
    if (JamePanel.currentPanel) {
      JamePanel.currentPanel.panel.reveal(vscode.ViewColumn.Beside);
      return;
    }

    const panel = vscode.window.createWebviewPanel(
      "jameWorkflowAgent",
      "JAME Workflow Agent",
      vscode.ViewColumn.Beside,
      {
        enableScripts: true,
      }
    );

    JamePanel.currentPanel = new JamePanel(panel, extensionUri);
  }

  private constructor(panel: vscode.WebviewPanel, _extensionUri: vscode.Uri) {
    this.panel = panel;
    this.panel.webview.html = this.getHtml();

    this.panel.onDidDispose(() => this.dispose(), null, this.disposables);

    this.panel.webview.onDidReceiveMessage(async (message: unknown) => {
      const msg = message as {
        command?: string;
        userRequest?: string;
        toolCallId?: string;
        action?: string;
        runId?: string;
        backendUrl?: string;
      };

      if (msg.command === "toolResponse") {
        const { toolCallId, action, runId, backendUrl } = msg;
        if (!toolCallId || !action || !runId || !backendUrl) { return; }
        const fetchFn = (globalThis as { fetch?: (input: string, init?: unknown) => Promise<any> }).fetch;
        if (!fetchFn) { return; }
        await fetchFn(`${backendUrl}/runs/${runId}/tool-response`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ tool_call_id: toolCallId, action }),
        }).catch(() => undefined);
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
        this.panel.webview.postMessage({
          command: "runCreated",
          runId: data.run_id,
          backendUrl,
        });
      } catch (error) {
        this.panel.webview.postMessage({
          command: "error",
          message: error instanceof Error ? error.message : String(error),
        });
      }
    });
  }

  public dispose(): void {
    JamePanel.currentPanel = undefined;
    this.panel.dispose();

    while (this.disposables.length) {
      const item = this.disposables.pop();
      if (item) {
        item.dispose();
      }
    }
  }

  private getHtml(): string {
    return `<!doctype html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <style>
    *, *::before, *::after { box-sizing: border-box; }
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; padding: 12px; margin: 0; }
    textarea { width: 100%; min-height: 120px; margin-bottom: 8px; }
    button { cursor: pointer; }
    .log { margin-top: 12px; max-height: 60vh; overflow-y: auto; border: 1px solid #3a3a3a; border-radius: 6px; padding: 8px; }
    .event { margin: 3px 0; font-size: 12px; color: #ccc; }

    /* ── Tool card (Copilot / Claude Code style) ────────────────── */
    .tool-card {
      margin: 8px 0;
      border: 1px solid #3c3c3c;
      border-radius: 8px;
      overflow: visible;
      font-size: 13px;
      background: #1f1f1f;
      position: relative;
    }
    /* "Running \`ruff check .\`" label above the card */
    .tool-card-label {
      font-size: 12px; color: #888;
      margin-bottom: 4px; padding-left: 2px;
    }
    .tool-card-label code {
      font-family: "SF Mono", Menlo, Consolas, monospace;
      color: #ccc; background: transparent;
    }
    /* Header row: terminal icon + "Run zsh command?" */
    .tool-card-header {
      padding: 9px 12px;
      display: flex; align-items: center; gap: 8px;
      color: #cccccc; font-size: 13px;
      border-bottom: 1px solid #3c3c3c;
    }
    .tc-icon {
      display: flex; align-items: center; justify-content: center;
      width: 22px; height: 22px;
      border: 1px solid #555; border-radius: 5px;
      font-size: 13px; color: #aaa; flex-shrink: 0;
    }
    .tc-shell {
      background: #3a3a3a; color: #ccc;
      border-radius: 4px; padding: 1px 7px;
      font-size: 11px; font-weight: 600;
    }
    /* Command body */
    .tool-card-command {
      padding: 10px 14px;
      font-family: "SF Mono", Menlo, Consolas, monospace;
      font-size: 13px; line-height: 1.5;
      word-break: break-all;
      border-bottom: 1px solid #3c3c3c;
    }
    .cmd-name { color: #ce9178; }   /* orange — matches Copilot */
    .cmd-args { color: #9cdcfe; }   /* light blue for args */
    /* Actions row */
    .tool-card-actions {
      padding: 9px 12px;
      display: flex; align-items: center; gap: 8px;
    }
    /* Split Allow | ∨ button */
    .split-btn { display: flex; align-items: stretch; position: relative; }
    .btn-allow {
      background: #0e78d5; color: #fff; border: none;
      border-radius: 4px 0 0 4px;
      padding: 5px 16px; font-size: 13px; font-weight: 500;
    }
    .btn-allow:hover { background: #1a8ae0; }
    .btn-chevron {
      background: #0e78d5; color: #fff; border: none;
      border-left: 1px solid rgba(255,255,255,0.2);
      border-radius: 0 4px 4px 0;
      padding: 5px 8px; font-size: 10px; line-height: 1;
    }
    .btn-chevron:hover { background: #1a8ae0; }
    /* Dropdown */
    .tc-dropdown {
      display: none; position: absolute;
      bottom: calc(100% + 4px); left: 0;
      background: #2d2d2d; border: 1px solid #484848;
      border-radius: 6px; z-index: 50; min-width: 200px;
      box-shadow: 0 6px 20px rgba(0,0,0,0.55);
      overflow: hidden;
    }
    .tc-dropdown.open { display: block; }
    .tc-dropdown-item {
      padding: 9px 14px; font-size: 12.5px; color: #ccc;
      cursor: pointer; display: flex; align-items: center; gap: 8px;
    }
    .tc-dropdown-item:hover { background: #383838; }
    /* Skip button */
    .btn-skip {
      background: transparent; color: #cccccc;
      border: 1px solid #505050; border-radius: 4px;
      padding: 5px 16px; font-size: 13px;
    }
    .btn-skip:hover { background: #2a2a2a; }
    /* Settled badges */
    .tc-badge {
      display: inline-flex; align-items: center; gap: 5px;
      border-radius: 4px; padding: 3px 10px;
      font-size: 12px; font-weight: 600;
    }
    .tc-badge.allowed { background: #1b3a1b; color: #4ec94e; border: 1px solid #2d5a2d; }
    .tc-badge.skipped { background: #3a1b1b; color: #f47067; border: 1px solid #5a2d2d; }
    .tc-badge.auto    { background: #1b2d3a; color: #4fc1ff; border: 1px solid #2d4a5a; }
    /* Tool result */
    .tool-card-result {
      margin: 0 12px 10px;
      padding: 7px 10px;
      background: #141414; border-radius: 5px;
      font-family: "SF Mono", Menlo, Consolas, monospace;
      font-size: 11.5px; color: #89d185;
      white-space: pre-wrap; word-break: break-all;
      max-height: 140px; overflow-y: auto;
      border: 1px solid #2c2c2c;
    }
    .tool-card-result.fail { color: #f47067; }

    /* ── Auto-approve warning modal ──────────────────────────────── */
    .modal-overlay {
      display: none;
      position: fixed; inset: 0;
      background: rgba(0,0,0,0.65);
      z-index: 100;
      align-items: center; justify-content: center;
    }
    .modal-overlay.open { display: flex; }
    .modal {
      background: #1e1e1e; border: 1px solid #454545;
      border-radius: 10px; padding: 24px;
      max-width: 380px; width: 90%;
      box-shadow: 0 8px 32px rgba(0,0,0,0.6);
    }
    .modal-title {
      display: flex; align-items: center; gap: 10px;
      font-size: 15px; font-weight: 600; color: #e8e8e8;
      margin-bottom: 12px;
    }
    .modal-title .shield { font-size: 20px; color: #2f6feb; }
    .modal-body { font-size: 12.5px; color: #bbb; line-height: 1.6; margin-bottom: 18px; }
    .modal-body strong { color: #e8e8e8; }
    .modal-actions { display: flex; justify-content: flex-end; gap: 10px; }
    .btn-modal-cancel {
      background: transparent; color: #ccc;
      border: 1px solid #555; border-radius: 6px;
      padding: 6px 16px; font-size: 13px;
    }
    .btn-modal-cancel:hover { background: #2a2d2e; }
    .btn-modal-enable {
      background: #2f6feb; color: #fff; border: none;
      border-radius: 6px; padding: 6px 16px; font-size: 13px; font-weight: 600;
    }
    .btn-modal-enable:hover { background: #388bfd; }

    #start { padding: 8px 14px; }
  </style>
</head>
<body>
  <h2>JAME Workflow Agent</h2>
  <p>Submit product intent and stream Plan/Act/Reason traces.</p>
  <textarea id="req" placeholder="Describe the app to generate..."></textarea>
  <button id="start">Start Run</button>
  <div class="log" id="log"></div>

  <!-- Auto-approve warning modal -->
  <div class="modal-overlay" id="autoApproveModal">
    <div class="modal">
      <div class="modal-title">
        <span class="shield">🛡</span>
        Enable auto-approve for QA tools?
      </div>
      <div class="modal-body">
        All subsequent <strong>ruff</strong> and <strong>pytest</strong> commands
        in this run will execute automatically without asking.<br><br>
        These are read-only analysis tools and do <strong>not</strong> modify your
        files, but you should only enable this if you trust the generated code.
      </div>
      <div class="modal-actions">
        <button class="btn-modal-cancel" id="modalCancel">Cancel</button>
        <button class="btn-modal-enable" id="modalEnable">Enable</button>
      </div>
    </div>
  </div>

  <script>
    const vscode = acquireVsCodeApi();
    const log = document.getElementById("log");
    const req = document.getElementById("req");
    const modal = document.getElementById("autoApproveModal");
    let ws;
    let currentRunId = null;
    let currentBackendUrl = null;
    let autoApprove = false;
    // toolCallId waiting for modal confirmation
    let pendingAutoToolCallId = null;
    let pendingAutoCard = null;
    let pendingAutoBody = null;

    /* ── Utilities ─────────────────────────────────────────────── */
    function esc(s) {
      return String(s)
        .replace(/&/g,"&amp;").replace(/</g,"&lt;")
        .replace(/>/g,"&gt;").replace(/"/g,"&quot;");
    }

    function append(text) {
      const el = document.createElement("div");
      el.className = "event";
      el.textContent = text;
      log.appendChild(el);
      log.scrollTop = log.scrollHeight;
    }

    /* ── Tool card ─────────────────────────────────────────────── */
    function appendToolCard(data) {
      const payload    = data.payload || {};
      const toolName   = payload.tool_name || "tool";
      const cmdBin     = payload.command || toolName;
      // args[0] is the bin path itself (same as command); real args start at [1]
      const rawArgs    = payload.args || [];
      const displayArgs = rawArgs.length > 1 ? rawArgs.slice(1) : rawArgs;
      const toolCallId = payload.tool_call_id;

      // Short command name (basename of bin path)
      const cmdShort = cmdBin.split("/").pop() || cmdBin;
      // Display string for label: "ruff check ."
      const displayCmd = [cmdShort, ...displayArgs].join(" ");

      // Label above card: Running "ruff check ."
      const label = document.createElement("div");
      label.className = "tool-card-label";
      label.innerHTML = "Running <code>" + esc(displayCmd) + "</code>";
      log.appendChild(label);

      const card = document.createElement("div");
      card.className = "tool-card";
      card.dataset.toolCallId = toolCallId;
      card.dataset.toolName   = toolName;
      card.innerHTML =
        '<div class="tool-card-header">' +
          '<span class="tc-icon">&#x2395;</span>' +
          'Run <span class="tc-shell">zsh</span> command?' +
        '</div>' +
        '<div class="tool-card-command">' +
          '<span class="cmd-name">' + esc(cmdShort) + '</span>' +
          (displayArgs.length ? ' <span class="cmd-args">' + esc(displayArgs.join(" ")) + '</span>' : '') +
        '</div>' +
        '<div class="tool-card-actions">' +
          '<div class="split-btn">' +
            '<button class="btn-allow">Allow</button>' +
            '<button class="btn-chevron" title="More options">&#9660;</button>' +
            '<div class="tc-dropdown">' +
              '<div class="tc-dropdown-item" data-action="auto-approve">' +
                '&#x2713;&nbsp; Enable auto-approve' +
              '</div>' +
            '</div>' +
          '</div>' +
          '<button class="btn-skip">Skip</button>' +
        '</div>';

      log.appendChild(card);
      log.scrollTop = log.scrollHeight;

      const body    = card.querySelector(".tool-card-actions");
      const chevron = card.querySelector(".btn-chevron");
      const dropdown= card.querySelector(".tc-dropdown");

      // If auto-approve already on, fire immediately
      if (autoApprove) {
        _settle(card, body, toolCallId, "run", true);
        return;
      }

      card.querySelector(".btn-allow").addEventListener("click", () => {
        _settle(card, body, toolCallId, "run", false);
      });

      card.querySelector(".btn-skip").addEventListener("click", () => {
        _settle(card, body, toolCallId, "skip", false);
      });

      chevron.addEventListener("click", (e) => {
        e.stopPropagation();
        dropdown.classList.toggle("open");
      });

      card.querySelector('[data-action="auto-approve"]').addEventListener("click", () => {
        dropdown.classList.remove("open");
        // show warning modal before enabling
        pendingAutoToolCallId = toolCallId;
        pendingAutoCard = card;
        pendingAutoBody = body;
        modal.classList.add("open");
      });

      // close dropdown on outside click
      document.addEventListener("click", function closeDD() {
        dropdown.classList.remove("open");
      }, { once: true });
    }

    function _settle(card, actionsEl, toolCallId, action, isAuto) {
      actionsEl.querySelectorAll("button").forEach((b) => {
        b.disabled = true; b.style.opacity = "0.4";
      });
      actionsEl.querySelectorAll(".split-btn").forEach((s) => s.style.opacity = "0.4");

      const badge = document.createElement("span");
      if (isAuto) {
        badge.className = "tc-badge auto";
        badge.textContent = "⚡ Auto-approved";
      } else if (action === "run") {
        badge.className = "tc-badge allowed";
        badge.textContent = "✓ Allowed";
      } else {
        badge.className = "tc-badge skipped";
        badge.textContent = "✕ Skipped";
      }
      actionsEl.appendChild(badge);

      vscode.postMessage({
        command: "toolResponse",
        toolCallId,
        action,
        runId: currentRunId,
        backendUrl: currentBackendUrl,
      });
    }

    /* ── Tool result ───────────────────────────────────────────── */
    function appendToolResult(data) {
      const toolResult = (data.payload || {}).tool_result;
      if (!toolResult) return;
      const { tool_name, exit_code, output } = toolResult;

      // Find the most-recent card for this tool name
      let targetCard = null;
      log.querySelectorAll(".tool-card").forEach((c) => {
        if (c.dataset.toolName === tool_name) { targetCard = c; }
      });

      const passed = exit_code === 0;
      const resultEl = document.createElement("div");
      resultEl.className = "tool-card-result" + (passed ? "" : " fail");
      resultEl.textContent =
        (passed ? "✅ Passed" : "❌ Failed (exit " + exit_code + ")") +
        (output ? "\\n" + String(output).slice(0, 900) : "");

      if (targetCard) {
        targetCard.appendChild(resultEl);
      } else {
        append("[" + tool_name + "] exit=" + exit_code);
      }
      log.scrollTop = log.scrollHeight;
    }

    /* ── Modal ─────────────────────────────────────────────────── */
    document.getElementById("modalCancel").addEventListener("click", () => {
      modal.classList.remove("open");
      pendingAutoToolCallId = pendingAutoCard = pendingAutoBody = null;
    });

    document.getElementById("modalEnable").addEventListener("click", () => {
      modal.classList.remove("open");
      autoApprove = true;
      if (pendingAutoToolCallId && pendingAutoCard && pendingAutoBody) {
        _settle(pendingAutoCard, pendingAutoBody, pendingAutoToolCallId, "run", true);
      }
      pendingAutoToolCallId = pendingAutoCard = pendingAutoBody = null;
    });

    /* ── Run start ─────────────────────────────────────────────── */
    document.getElementById("start").addEventListener("click", () => {
      const userRequest = req.value.trim();
      if (!userRequest) { append("Please enter a request."); return; }
      log.innerHTML = "";
      autoApprove = false;
      append("Creating run...");
      vscode.postMessage({ command: "startRun", userRequest });
    });

    /* ── Messages from extension host ─────────────────────────── */
    window.addEventListener("message", (event) => {
      const msg = event.data;
      if (msg.command === "error") {
        append("ERROR: " + msg.message);
      }
      if (msg.command === "runCreated") {
        currentRunId      = msg.runId;
        currentBackendUrl = msg.backendUrl;
        append("Run created: " + msg.runId);
        const wsUrl = msg.backendUrl
          .replace("http://","ws://").replace("https://","wss://")
          + "/ws/runs/" + msg.runId;
        ws = new WebSocket(wsUrl);
        ws.onmessage = (evt) => {
          const data = JSON.parse(evt.data);
          if (data.event === "tool_call") {
            appendToolCard(data);
          } else if (data.event === "agent_update" && data.payload && data.payload.tool_result) {
            appendToolResult(data);
          } else {
            append("[" + data.event + "] " + data.message);
          }
        };
        ws.onclose = () => append("Event stream closed.");
        ws.onerror = () => append("WebSocket error.");
      }
    });
  </script>
</body>
</html>`;
  }
}
