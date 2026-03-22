import * as vscode from "vscode";
import { JameViewProvider } from "./view";

export function activate(context: vscode.ExtensionContext): void {
  const provider = new JameViewProvider(context.extensionUri);

  context.subscriptions.push(
    vscode.window.registerWebviewViewProvider("jameWorkflow.agent", provider, {
      webviewOptions: { retainContextWhenHidden: true },
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("jameWorkflow.focus", () => {
      provider.focus();
    })
  );
}

export function deactivate(): void {}
