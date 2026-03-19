import React, {
  createContext,
  useContext,
  useReducer,
  useCallback,
  useEffect,
} from 'react';
import type {
  FileMap,
  ActionStates,
  AgentMappings,
  WorkspaceStateData,
  HistorySnapshot,
  Context,
  ContextMap,
} from '../types';

// ---- helpers ----

function emptyActionStates(): ActionStates {
  return {
    mcps: { expanded: false, active: false, items: {} },
    agents: { expanded: false, active: false, items: {} },
    rules: { expanded: false, active: false, items: {} },
  };
}

function snapshot(state: WorkspaceStateData): HistorySnapshot {
  return {
    files: { ...state.files },
    selectedFile: state.selectedFile,
    expandedFolders: Array.from(state.expandedFolders),
    actionStates: JSON.parse(JSON.stringify(state.actionStates)),
    agentMappings: { ...state.agentMappings },
    timestamp: new Date().toISOString(),
  };
}

function fromSnapshot(snap: HistorySnapshot): Partial<WorkspaceStateData> {
  return {
    files: { ...snap.files },
    selectedFile: snap.selectedFile,
    expandedFolders: new Set(snap.expandedFolders || []),
    actionStates: snap.actionStates
      ? JSON.parse(JSON.stringify(snap.actionStates))
      : emptyActionStates(),
    agentMappings: snap.agentMappings ? { ...snap.agentMappings } : {},
  };
}

function initEmpty(): WorkspaceStateData {
  const base: WorkspaceStateData = {
    files: {},
    selectedFile: null,
    expandedFolders: new Set(),
    actionStates: emptyActionStates(),
    agentMappings: {},
    history: { past: [], present: null, future: [], maxSize: 50 },
  };
  base.history.present = snapshot(base);
  return base;
}

function serializeState(contextId: string, state: WorkspaceStateData): string {
  return JSON.stringify({
    contextId,
    files: state.files,
    selectedFile: state.selectedFile,
    expandedFolders: Array.from(state.expandedFolders),
    actionStates: state.actionStates,
    agentMappings: state.agentMappings,
    history: {
      past: state.history.past,
      present: state.history.present,
      future: state.history.future,
    },
  });
}

function deserializeState(contextId: string, raw: string | null): WorkspaceStateData {
  if (!raw) return initEmpty();
  try {
    const parsed = JSON.parse(raw);
    const state = initEmpty();
    state.files = parsed.files || {};
    state.selectedFile = parsed.selectedFile || null;
    state.expandedFolders = new Set(parsed.expandedFolders || []);
    if (parsed.actionStates) state.actionStates = parsed.actionStates;
    if (parsed.agentMappings) state.agentMappings = parsed.agentMappings;
    if (parsed.history) {
      state.history.past = parsed.history.past || [];
      state.history.present = parsed.history.present || snapshot(state);
      state.history.future = parsed.history.future || [];
    } else {
      state.history.present = snapshot(state);
    }
    return state;
  } catch {
    return initEmpty();
  }
}

function loadContextsList(): ContextMap {
  try {
    const data = localStorage.getItem('app:contexts');
    return data ? JSON.parse(data) : {};
  } catch {
    return {};
  }
}

function saveContextsList(contexts: ContextMap) {
  localStorage.setItem('app:contexts', JSON.stringify(contexts));
}

// ---- reducer ----

type Action =
  | { type: 'ADD_FILE'; path: string; content: string }
  | { type: 'DELETE_FILE'; path: string }
  | { type: 'UPDATE_FILE'; path: string; content: string }
  | { type: 'SELECT_FILE'; path: string | null }
  | { type: 'UNDO' }
  | { type: 'REDO' }
  | { type: 'RESET' }
  | { type: 'TOGGLE_FOLDER'; path: string }
  | { type: 'SET_ACTION_CATEGORY'; category: keyof ActionStates; active: boolean; expanded?: boolean }
  | { type: 'SET_ACTION_ITEM'; category: keyof ActionStates; itemName: string; checked: boolean }
  | { type: 'SET_AGENT_MAPPING'; agentName: string; filePath: string }
  | { type: 'LOAD_STATE'; state: WorkspaceStateData };

function pushHistory(state: WorkspaceStateData): WorkspaceStateData {
  if (!state.history.present) return state;
  const past = [...state.history.past, state.history.present];
  if (past.length > state.history.maxSize) past.shift();
  return { ...state, history: { ...state.history, past, future: [] } };
}

function workspaceReducer(state: WorkspaceStateData, action: Action): WorkspaceStateData {
  switch (action.type) {
    case 'ADD_FILE': {
      const next = pushHistory(state);
      const files = { ...next.files, [action.path]: action.content };
      const newState = { ...next, files, selectedFile: action.path };
      return { ...newState, history: { ...newState.history, present: snapshot(newState) } };
    }
    case 'DELETE_FILE': {
      const next = pushHistory(state);
      const files = { ...next.files };
      delete files[action.path];
      const selectedFile = next.selectedFile === action.path ? null : next.selectedFile;
      const newState = { ...next, files, selectedFile };
      return { ...newState, history: { ...newState.history, present: snapshot(newState) } };
    }
    case 'UPDATE_FILE': {
      if (!state.files[action.path]) return state;
      const files = { ...state.files, [action.path]: action.content };
      return { ...state, files };
    }
    case 'SELECT_FILE': {
      return { ...state, selectedFile: action.path };
    }
    case 'UNDO': {
      if (state.history.past.length === 0) return state;
      const past = [...state.history.past];
      const previousSnap = past.pop()!;
      const future = [state.history.present!, ...state.history.future];
      const restored = fromSnapshot(previousSnap);
      return {
        ...state,
        ...restored,
        history: { ...state.history, past, present: previousSnap, future },
      };
    }
    case 'REDO': {
      if (state.history.future.length === 0) return state;
      const future = [...state.history.future];
      const nextSnap = future.shift()!;
      const past = [...state.history.past, state.history.present!];
      const restored = fromSnapshot(nextSnap);
      return {
        ...state,
        ...restored,
        history: { ...state.history, past, present: nextSnap, future },
      };
    }
    case 'RESET': {
      const next = pushHistory(state);
      const newState = {
        ...next,
        files: {},
        selectedFile: null,
        expandedFolders: new Set<string>(),
        actionStates: emptyActionStates(),
        agentMappings: {},
      };
      return { ...newState, history: { ...newState.history, present: snapshot(newState) } };
    }
    case 'TOGGLE_FOLDER': {
      const expandedFolders = new Set(state.expandedFolders);
      const key = action.path + ':collapsed';
      if (expandedFolders.has(key)) {
        expandedFolders.delete(key);
      } else {
        expandedFolders.add(key);
      }
      return { ...state, expandedFolders };
    }
    case 'SET_ACTION_CATEGORY': {
      const actionStates = JSON.parse(JSON.stringify(state.actionStates)) as ActionStates;
      actionStates[action.category].active = action.active;
      if (action.expanded !== undefined) {
        actionStates[action.category].expanded = action.expanded;
      }
      return { ...state, actionStates };
    }
    case 'SET_ACTION_ITEM': {
      const actionStates = JSON.parse(JSON.stringify(state.actionStates)) as ActionStates;
      actionStates[action.category].items[action.itemName] = action.checked;
      return { ...state, actionStates };
    }
    case 'SET_AGENT_MAPPING': {
      return {
        ...state,
        agentMappings: { ...state.agentMappings, [action.agentName]: action.filePath },
      };
    }
    case 'LOAD_STATE': {
      return action.state;
    }
    default:
      return state;
  }
}

// ---- context ----

interface WorkspaceContextValue {
  workspaceState: WorkspaceStateData;
  currentContextId: string;
  contexts: ContextMap;
  dispatch: React.Dispatch<Action>;
  includeFile: (path: string, content: string) => void;
  deleteFile: (path: string) => void;
  updateFile: (path: string, content: string) => void;
  selectFile: (path: string | null) => void;
  undo: () => void;
  redo: () => void;
  reset: () => void;
  switchContext: (contextId: string) => void;
  createContext: (id: string, name: string) => boolean;
  deleteContext: (contextId: string) => void;
  setActionCategory: (category: keyof ActionStates, active: boolean, expanded?: boolean) => void;
  setActionItem: (category: keyof ActionStates, itemName: string, checked: boolean) => void;
  setAgentMapping: (agentName: string, filePath: string) => void;
  toggleFolder: (path: string) => void;
}

const WorkspaceContext = createContext<WorkspaceContextValue | null>(null);

export function WorkspaceProvider({ children }: { children: React.ReactNode }) {
  const [contexts, setContexts] = React.useState<ContextMap>(() => {
    const loaded = loadContextsList();
    if (!loaded['default']) {
      loaded['default'] = { id: 'default', name: 'Default Workspace', createdAt: Date.now() };
      saveContextsList(loaded);
    }
    return loaded;
  });

  const [currentContextId, setCurrentContextId] = React.useState<string>(() => {
    const last = localStorage.getItem('app:currentContext');
    const loaded = loadContextsList();
    return last && loaded[last] ? last : 'default';
  });

  const [workspaceState, dispatch] = useReducer(workspaceReducer, undefined, () => {
    const initialId = (() => {
      const last = localStorage.getItem('app:currentContext');
      const loaded = loadContextsList();
      return last && loaded[last] ? last : 'default';
    })();
    return deserializeState(initialId, localStorage.getItem(`app:workspace:${initialId}`));
  });

  // Save to localStorage whenever state changes
  useEffect(() => {
    localStorage.setItem(`app:workspace:${currentContextId}`, serializeState(currentContextId, workspaceState));
  }, [currentContextId, workspaceState]);

  const includeFile = useCallback((path: string, content: string) => {
    dispatch({ type: 'ADD_FILE', path, content });
  }, []);

  const deleteFile = useCallback((path: string) => {
    dispatch({ type: 'DELETE_FILE', path });
  }, []);

  const updateFile = useCallback((path: string, content: string) => {
    dispatch({ type: 'UPDATE_FILE', path, content });
  }, []);

  const selectFile = useCallback((path: string | null) => {
    dispatch({ type: 'SELECT_FILE', path });
  }, []);

  const undo = useCallback(() => {
    dispatch({ type: 'UNDO' });
  }, []);

  const redo = useCallback(() => {
    dispatch({ type: 'REDO' });
  }, []);

  const reset = useCallback(() => {
    dispatch({ type: 'RESET' });
  }, []);

  const toggleFolder = useCallback((path: string) => {
    dispatch({ type: 'TOGGLE_FOLDER', path });
  }, []);

  const setActionCategory = useCallback(
    (category: keyof ActionStates, active: boolean, expanded?: boolean) => {
      dispatch({ type: 'SET_ACTION_CATEGORY', category, active, expanded });
    },
    []
  );

  const setActionItem = useCallback(
    (category: keyof ActionStates, itemName: string, checked: boolean) => {
      dispatch({ type: 'SET_ACTION_ITEM', category, itemName, checked });
    },
    []
  );

  const setAgentMapping = useCallback((agentName: string, filePath: string) => {
    dispatch({ type: 'SET_AGENT_MAPPING', agentName, filePath });
  }, []);

  const switchContext = useCallback(
    (contextId: string) => {
      // Save current state first
      localStorage.setItem(`app:workspace:${currentContextId}`, serializeState(currentContextId, workspaceState));

      // Load new state
      const newState = deserializeState(contextId, localStorage.getItem(`app:workspace:${contextId}`));
      dispatch({ type: 'LOAD_STATE', state: newState });
      setCurrentContextId(contextId);
      localStorage.setItem('app:currentContext', contextId);
    },
    [currentContextId, workspaceState]
  );

  const createContext = useCallback(
    (id: string, name: string): boolean => {
      const current = loadContextsList();
      if (current[id]) return false;
      current[id] = { id, name, createdAt: Date.now() };
      saveContextsList(current);
      setContexts({ ...current });
      return true;
    },
    []
  );

  const deleteContext = useCallback(
    (contextId: string) => {
      if (contextId === 'default') return;
      const current = loadContextsList();
      delete current[contextId];
      localStorage.removeItem(`app:workspace:${contextId}`);
      saveContextsList(current);
      setContexts({ ...current });

      if (currentContextId === contextId) {
        const newState = deserializeState('default', localStorage.getItem('app:workspace:default'));
        dispatch({ type: 'LOAD_STATE', state: newState });
        setCurrentContextId('default');
        localStorage.setItem('app:currentContext', 'default');
      }
    },
    [currentContextId]
  );

  // Keep contexts in sync with localStorage
  useEffect(() => {
    const loaded = loadContextsList();
    if (!loaded['default']) {
      loaded['default'] = { id: 'default', name: 'Default Workspace', createdAt: Date.now() };
      saveContextsList(loaded);
    }
    setContexts(loaded);
  }, [currentContextId]);

  const value: WorkspaceContextValue = {
    workspaceState,
    currentContextId,
    contexts,
    dispatch,
    includeFile,
    deleteFile,
    updateFile,
    selectFile,
    undo,
    redo,
    reset,
    switchContext,
    createContext,
    deleteContext,
    setActionCategory,
    setActionItem,
    setAgentMapping,
    toggleFolder,
  };

  return <WorkspaceContext.Provider value={value}>{children}</WorkspaceContext.Provider>;
}

export function useWorkspace(): WorkspaceContextValue {
  const ctx = useContext(WorkspaceContext);
  if (!ctx) throw new Error('useWorkspace must be used within WorkspaceProvider');
  return ctx;
}
