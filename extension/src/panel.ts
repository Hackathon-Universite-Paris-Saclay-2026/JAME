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
      const msg = message as { command?: string; userRequest?: string };
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
    body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; padding: 12px; }
    textarea { width: 100%; min-height: 120px; margin-bottom: 8px; }
    button { padding: 8px 10px; }
    .log { margin-top: 12px; max-height: 300px; overflow-y: auto; border: 1px solid #666; padding: 8px; }
    .event { margin: 4px 0; font-size: 12px; }
  </style>
</head>
<body>
  <h2>JAME Workflow Agent</h2>
  <p>Submit product intent and stream Plan/Act/Reason traces.</p>
  <textarea id="req" placeholder="Describe the app to generate..."></textarea>
  <button id="start">Start Run</button>
  <div class="log" id="log"></div>

  <script>
    const vscode = acquireVsCodeApi();
    const log = document.getElementById("log");
    const req = document.getElementById("req");
    const start = document.getElementById("start");
    let ws;

    function append(text) {
      const el = document.createElement("div");
      el.className = "event";
      el.textContent = text;
      log.appendChild(el);
      log.scrollTop = log.scrollHeight;
    }

    start.addEventListener("click", () => {
      const userRequest = req.value.trim();
      if (!userRequest) {
        append("Please enter a request.");
        return;
      }
      log.innerHTML = "";
      append("Creating run...");
      vscode.postMessage({ command: "startRun", userRequest });
    });

    window.addEventListener("message", (event) => {
      const msg = event.data;
      if (msg.command === "error") {
        append("ERROR: " + msg.message);
      }
      if (msg.command === "runCreated") {
        append("Run created: " + msg.runId);
        const wsUrl = msg.backendUrl.replace("http://", "ws://").replace("https://", "wss://") + "/ws/runs/" + msg.runId;
        ws = new WebSocket(wsUrl);
        ws.onmessage = (evt) => {
          const data = JSON.parse(evt.data);
          append("[" + data.event + "] " + data.message);
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
