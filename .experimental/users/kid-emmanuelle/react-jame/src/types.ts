export interface FileMap {
  [path: string]: string;
}

export interface ActionCategoryState {
  expanded: boolean;
  active: boolean;
  items: { [name: string]: boolean };
}

export interface ActionStates {
  mcps: ActionCategoryState;
  agents: ActionCategoryState;
  rules: ActionCategoryState;
}

export interface AgentMappings {
  [agentName: string]: string;
}

export interface HistorySnapshot {
  files: FileMap;
  selectedFile: string | null;
  expandedFolders: string[];
  actionStates: ActionStates;
  agentMappings: AgentMappings;
  timestamp: string;
}

export interface WorkspaceStateData {
  files: FileMap;
  selectedFile: string | null;
  expandedFolders: Set<string>;
  actionStates: ActionStates;
  agentMappings: AgentMappings;
  history: {
    past: HistorySnapshot[];
    present: HistorySnapshot | null;
    future: HistorySnapshot[];
    maxSize: number;
  };
}

export interface Context {
  id: string;
  name: string;
  createdAt: number;
}

export interface ContextMap {
  [id: string]: Context;
}

export interface RuleItem {
  name: string;
  slug?: string;
  display_name?: string;
  tags?: string[];
  rulesets?: string[];
}

export interface AgentItem {
  name: string;
  slug?: string;
  display_name?: string;
  filename?: string;
  content?: string;
}

export interface McpItem {
  name: string;
  slug?: string;
  display_name?: string;
  config?: Record<string, unknown>;
}

export interface RulesetItem {
  name: string;
  display_name?: string;
  rules: RuleItem[];
}

export type TreeNode =
  | { type: 'file'; path: string; name: string }
  | { type: 'folder'; path: string; name: string; children: TreeNode[] };
