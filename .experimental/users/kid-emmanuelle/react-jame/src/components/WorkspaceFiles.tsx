import { useWorkspace } from '../context/WorkspaceContext';
import type { TreeNode } from '../types';

interface WorkspaceFilesProps {
  onOpenFileModal: (basePath?: string) => void;
  onOpenContextModal: () => void;
}

function buildTree(files: Record<string, string>): TreeNode[] {
  const tree: Record<string, TreeNode> = {};

  Object.keys(files).forEach((filePath) => {
    const parts = filePath.split('/');
    let current: Record<string, TreeNode> = tree;

    for (let i = 0; i < parts.length - 1; i++) {
      const folderName = parts[i];
      const folderPath = parts.slice(0, i + 1).join('/');
      if (!current[folderName]) {
        current[folderName] = {
          type: 'folder',
          path: folderPath,
          name: folderName,
          children: [],
        };
      }
      const node = current[folderName];
      if (node.t-ype === 'folder') {
        // Rebuild children map
        const childMap: Record<string, TreeNode> = {};
        node.children.forEach((c) => { childMap[c.name] = c; });
        current = childMap;
      }
    }

    const fileName = parts[parts.length - 1];
    current[fileName] = {
      type: 'file',
      path: filePath,
      name: fileName,
    };
  });

  function convertObj(obj: Record<string, TreeNode>): TreeNode[] {
    return Object.values(obj).map((node) => {
      if (node.type === 'folder') {
        return node;
      }
      return node;
    });
  }

  return convertObj(tree);
}

// Actually build the tree properly with nested folders
function buildTreeProperly(files: Record<string, string>): TreeNode[] {
  interface FolderNode {
    type: 'folder';
    path: string;
    name: string;
    childMap: Record<string, FolderNode | FileNode>;
  }
  interface FileNode {
    type: 'file';
    path: string;
    name: string;
  }

  const root: Record<string, FolderNode | FileNode> = {};

  Object.keys(files).forEach((filePath) => {
    const parts = filePath.split('/');
    let current: Record<string, FolderNode | FileNode> = root;

    for (let i = 0; i < parts.length - 1; i++) {
      const folderName = parts[i];
      const folderPath = parts.slice(0, i + 1).join('/');
      if (!current[folderName]) {
        current[folderName] = {
          type: 'folder',
          path: folderPath,
          name: folderName,
          childMap: {},
        };
      }
      current = (current[folderName] as FolderNode).childMap;
    }

    const fileName = parts[parts.length - 1];
    current[fileName] = { type: 'file', path: filePath, name: fileName };
  });

  function toNodes(map: Record<string, FolderNode | FileNode>): TreeNode[] {
    return Object.values(map).map((node) => {
      if (node.type === 'folder') {
        return {
          type: 'folder' as const,
          path: node.path,
          name: node.name,
          children: toNodes(node.childMap),
        };
      }
      return { type: 'file' as const, path: node.path, name: node.name };
    });
  }

  return toNodes(root);
}

interface TreeNodeComponentProps {
  node: TreeNode;
  level: number;
  onFileClick: (path: string) => void;
  onDeleteFile: (path: string) => void;
  onDeleteFolder: (path: string) => void;
  onAddFileToFolder: (path: string) => void;
  onToggleFolder: (path: string) => void;
  selectedFile: string | null;
  expandedFolders: Set<string>;
}

function TreeNodeComponent({
  node,
  level,
  onFileClick,
  onDeleteFile,
  onDeleteFolder,
  onAddFileToFolder,
  onToggleFolder,
  selectedFile,
  expandedFolders,
}: TreeNodeComponentProps) {
  if (node.type === 'folder') {
    const isCollapsed = expandedFolders.has(node.path + ':collapsed');
    const isExpanded = !isCollapsed;

    return (
      <div className={`tree-item level-${level}`}>
        <div className="flex items-center justify-between p-1 hover:bg-gray-100 group">
          <div
            className="flex items-center gap-1 cursor-pointer"
            onClick={() => onToggleFolder(node.path)}
          >
            <svg
              className={`w-3 h-3 transition-transform ${isExpanded ? '' : '-rotate-90'}`}
              fill="currentColor"
              viewBox="0 0 20 20"
            >
              <path d="M6 10l4 4 4-4" stroke="currentColor" strokeWidth="2" fill="none" />
            </svg>
            <span className={`mdi ${isExpanded ? 'mdi-folder-open' : 'mdi-folder'} text-blue-600 text-base`} />
            <span className="font-medium">{node.name}</span>
          </div>
          <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100">
            <button
              className="text-green-600 hover:text-green-800 p-1"
              onClick={() => onAddFileToFolder(node.path)}
              title="Add file to folder"
            >
              <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 4v16m8-8H4" />
              </svg>
            </button>
            <button
              className="text-red-600 hover:text-red-800 p-1"
              onClick={() => onDeleteFolder(node.path)}
              title="Delete folder"
            >
              <svg className="w-3 h-3" fill="currentColor" viewBox="0 0 20 20">
                <path
                  fillRule="evenodd"
                  d="M9 2a1 1 0 00-.894.553L7.382 4H4a1 1 0 000 2v10a2 2 0 002 2h8a2 2 0 002-2V6a1 1 0 100-2h-3.382l-.724-1.447A1 1 0 0011 2H9zM7 8a1 1 0 012 0v6a1 1 0 11-2 0V8zm5-1a1 1 0 00-1 1v6a1 1 0 102 0V8a1 1 0 00-1-1z"
                  clipRule="evenodd"
                />
              </svg>
            </button>
          </div>
        </div>
        {isExpanded &&
          node.children.map((child) => (
            <TreeNodeComponent
              key={child.path}
              node={child}
              level={level + 1}
              onFileClick={onFileClick}
              onDeleteFile={onDeleteFile}
              onDeleteFolder={onDeleteFolder}
              onAddFileToFolder={onAddFileToFolder}
              onToggleFolder={onToggleFolder}
              selectedFile={selectedFile}
              expandedFolders={expandedFolders}
            />
          ))}
      </div>
    );
  }

  // File node
  const isSelected = selectedFile === node.path;

  return (
    <div className={`tree-item level-${level}`}>
      <div
        className={`flex items-center justify-between p-1 hover:bg-gray-100 cursor-pointer group ${
          isSelected ? 'bg-cyan-100 font-medium' : ''
        }`}
        onClick={() => onFileClick(node.path)}
      >
        <div className="flex items-center gap-1">
          <span className="mdi mdi-file-document-outline text-gray-600 text-sm" />
          <span>{node.name}</span>
        </div>
        <button
          className="opacity-0 group-hover:opacity-100 hover:opacity-100 text-red-600 hover:text-red-800 p-1"
          onClick={(e) => {
            e.stopPropagation();
            onDeleteFile(node.path);
          }}
          title="Delete file"
        >
          <svg className="w-3 h-3" fill="currentColor" viewBox="0 0 20 20">
            <path
              fillRule="evenodd"
              d="M9 2a1 1 0 00-.894.553L7.382 4H4a1 1 0 000 2v10a2 2 0 002 2h8a2 2 0 002-2V6a1 1 0 100-2h-3.382l-.724-1.447A1 1 0 0011 2H9zM7 8a1 1 0 012 0v6a1 1 0 11-2 0V8zm5-1a1 1 0 00-1 1v6a1 1 0 102 0V8a1 1 0 00-1-1z"
              clipRule="evenodd"
            />
          </svg>
        </button>
      </div>
    </div>
  );
}

export default function WorkspaceFiles({ onOpenFileModal, onOpenContextModal }: WorkspaceFilesProps) {
  const {
    workspaceState,
    currentContextId,
    contexts,
    selectFile,
    deleteFile,
    toggleFolder,
    undo,
    redo,
    reset,
    switchContext,
    deleteContext,
  } = useWorkspace();

  const { files, selectedFile, expandedFolders, history } = workspaceState;
  const hasFiles = Object.keys(files).length > 0;
  const canUndo = history.past.length > 0;
  const canRedo = history.future.length > 0;

  function handleDeleteFolder(folderPath: string) {
    const prefix = folderPath + '/';
    const toDelete = Object.keys(files).filter((p) => p.startsWith(prefix));
    toDelete.forEach((p) => deleteFile(p));
  }

  function handleAddFileToFolder(folderPath: string) {
    onOpenFileModal(folderPath);
  }

  function getDefaultContent(fileName: string): string {
    if (fileName.endsWith('.md'))
      return `# ${fileName.replace('.md', '').replace(/[_-]/g, ' ')}\n\nYour content here...`;
    if (fileName.endsWith('.json')) return '{\n  "name": "new-file",\n  "version": "1.0.0"\n}';
    if (fileName.endsWith('.yaml') || fileName.endsWith('.yml'))
      return 'name: new-file\nversion: 1.0.0\n';
    return '# New file\n\nContent goes here...';
  }

  const treeNodes = buildTreeProperly(files);

  return (
    <div className="workspace-sidebar border-r-2 border-black flex flex-col" style={{ height: '100%' }}>
      {/* Header */}
      <div className="flex justify-between items-center mb-3 px-4 pt-4">
        <h3 className="text-lg font-black text-black">Files</h3>
        <div className="flex gap-2">
          <button
            id="files-prev-btn"
            onClick={undo}
            disabled={!canUndo}
            title="Previous"
            className="w-6 h-6 bg-gray-200 border-2 border-black text-black font-bold flex items-center justify-center shadow-[2px_2px_0px_0px_rgba(0,0,0,1)] disabled:opacity-50 disabled:cursor-not-allowed disabled:shadow-[1px_1px_0px_0px_rgba(0,0,0,1)] hover:shadow-[3px_3px_0px_0px_rgba(0,0,0,1)] hover:translate-x-[-1px] hover:translate-y-[-1px] active:shadow-[1px_1px_0px_0px_rgba(0,0,0,1)] active:translate-x-[1px] active:translate-y-[1px] transition-all"
          >
            <span className="mdi mdi-chevron-left text-sm" />
          </button>
          <button
            id="files-next-btn"
            onClick={redo}
            disabled={!canRedo}
            title="Next"
            className="w-6 h-6 bg-gray-200 border-2 border-black text-black font-bold flex items-center justify-center shadow-[2px_2px_0px_0px_rgba(0,0,0,1)] disabled:opacity-50 disabled:cursor-not-allowed disabled:shadow-[1px_1px_0px_0px_rgba(0,0,0,1)] hover:shadow-[3px_3px_0px_0px_rgba(0,0,0,1)] hover:translate-x-[-1px] hover:translate-y-[-1px] active:shadow-[1px_1px_0px_0px_rgba(0,0,0,1)] active:translate-x-[1px] active:translate-y-[1px] transition-all"
          >
            <span className="mdi mdi-chevron-right text-sm" />
          </button>
          <button
            id="files-reset-btn"
            onClick={reset}
            title="Reset"
            className="w-6 h-6 bg-red-400 border-2 border-black text-black font-bold flex items-center justify-center shadow-[2px_2px_0px_0px_rgba(0,0,0,1)] hover:shadow-[3px_3px_0px_0px_rgba(0,0,0,1)] hover:translate-x-[-1px] hover:translate-y-[-1px] active:shadow-[1px_1px_0px_0px_rgba(0,0,0,1)] active:translate-x-[1px] active:translate-y-[1px] transition-all"
          >
            <span className="mdi mdi-trash-can-outline text-sm" />
          </button>
        </div>
      </div>

      {/* File tree */}
      <div className="flex-1 overflow-hidden flex flex-col px-4 pb-3" style={{ minHeight: 0 }}>
        <div
          className="bg-white border-2 border-black p-3 mb-3 overflow-y-auto"
          style={{ minHeight: 200, maxHeight: 900 }}
        >
          {/* Root folder label */}
          <div className="flex items-center p-1 text-gray-400 pointer-events-none" style={{ paddingLeft: 4 }}>
            <div className="flex items-center gap-1">
              <span className="mdi mdi-folder-open text-gray-400 text-base" />
              <span className="font-medium">/your-repo</span>
            </div>
          </div>

          {!hasFiles && (
            <div className="text-center text-gray-500 text-sm py-8">
              No files yet. Add tools from the right sidebar.
            </div>
          )}

          {treeNodes.map((node) => (
            <TreeNodeComponent
              key={node.path}
              node={node}
              level={0}
              onFileClick={selectFile}
              onDeleteFile={deleteFile}
              onDeleteFolder={handleDeleteFolder}
              onAddFileToFolder={handleAddFileToFolder}
              onToggleFolder={toggleFolder}
              selectedFile={selectedFile}
              expandedFolders={expandedFolders}
            />
          ))}
        </div>

        <div className="flex gap-2">
          <button
            onClick={() => onOpenFileModal()}
            className="w-full px-3 py-2 border-2 border-black text-black font-bold shadow-[2px_2px_0px_0px_rgba(0,0,0,1)] hover:shadow-[4px_4px_0px_0px_rgba(0,0,0,1)] hover:translate-x-[-1px] hover:translate-y-[-1px] active:shadow-[1px_1px_0px_0px_rgba(0,0,0,1)] active:translate-x-[1px] active:translate-y-[1px] transition-all flex items-center justify-center gap-1 text-sm rounded-none focus:outline-none focus:ring-0"
            style={{ backgroundColor: '#a7cefd' }}
          >
            <span className="mdi mdi-file-plus text-base" />
            <span>New File</span>
          </button>
        </div>

        <div className="flex-1" />
      </div>

      {/* Context selector */}
      <div className="p-4 border-t-2 border-black" style={{ flexShrink: 0 }}>
        <div className="flex flex-col gap-2">
          <div className="flex justify-between items-center mb-1">
            <label className="text-sm font-black text-black">CONTEXT</label>
            <div className="flex gap-1">
              <button
                onClick={onOpenContextModal}
                title="New Context"
                className="w-6 h-6 bg-pink-400 border-2 border-black text-black font-bold flex items-center justify-center shadow-[2px_2px_0px_0px_rgba(0,0,0,1)] hover:shadow-[3px_3px_0px_0px_rgba(0,0,0,1)] hover:translate-x-[-1px] hover:translate-y-[-1px] active:shadow-[1px_1px_0px_0px_rgba(0,0,0,1)] active:translate-x-[1px] active:translate-y-[1px] transition-all"
              >
                <span className="text-xs">+</span>
              </button>
              <button
                onClick={() => {
                  if (currentContextId !== 'default') {
                    deleteContext(currentContextId);
                  }
                }}
                title="Delete Context"
                className="w-6 h-6 bg-red-400 border-2 border-black text-black font-bold flex items-center justify-center shadow-[2px_2px_0px_0px_rgba(0,0,0,1)] hover:shadow-[3px_3px_0px_0px_rgba(0,0,0,1)] hover:translate-x-[-1px] hover:translate-y-[-1px] active:shadow-[1px_1px_0px_0px_rgba(0,0,0,1)] active:translate-x-[1px] active:translate-y-[1px] transition-all"
              >
                <span className="text-xs">×</span>
              </button>
            </div>
          </div>
          <select
            value={currentContextId}
            onChange={(e) => switchContext(e.target.value)}
            className="w-full px-2 py-2 bg-white border-2 border-black text-black font-medium shadow-[2px_2px_0px_0px_rgba(0,0,0,1)] hover:shadow-[3px_3px_0px_0px_rgba(0,0,0,1)] hover:translate-x-[-1px] hover:translate-y-[-1px] focus:shadow-[3px_3px_0px_0px_rgba(0,0,0,1)] focus:translate-x-[-1px] focus:translate-y-[-1px] focus:outline-none text-xs truncate transition-all appearance-none bg-no-repeat"
            style={{
              backgroundImage:
                "url('data:image/svg+xml;charset=UTF-8,%3csvg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 24 24%22%3e%3cpath fill=%22%23000000%22 d=%22M7 10l5 5 5-5z%22/%3e%3c/svg%3e')",
              backgroundPosition: 'right 0.5rem center',
              backgroundSize: '1.5em 1.5em',
              paddingRight: '2rem',
            }}
          >
            {Object.values(contexts).map((ctx) => {
              let displayName = ctx.name;
              if (displayName.length > 25) displayName = displayName.substring(0, 22) + '...';
              return (
                <option key={ctx.id} value={ctx.id} title={ctx.name}>
                  {displayName}
                </option>
              );
            })}
          </select>
        </div>
      </div>
    </div>
  );
}
