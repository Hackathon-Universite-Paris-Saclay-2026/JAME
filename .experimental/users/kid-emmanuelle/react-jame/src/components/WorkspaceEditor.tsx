import { useState } from 'react';
import Editor from '@monaco-editor/react';
import { useWorkspace } from '../context/WorkspaceContext';

function getLanguage(filePath: string | null): string {
  if (!filePath) return 'markdown';
  const ext = filePath.split('.').pop()?.toLowerCase();
  switch (ext) {
    case 'json': return 'json';
    case 'ts': case 'tsx': return 'typescript';
    case 'js': case 'jsx': return 'javascript';
    case 'py': return 'python';
    case 'yaml': case 'yml': return 'yaml';
    case 'sh': case 'bash': return 'shell';
    case 'md': return 'markdown';
    default: return 'markdown';
  }
}

export default function WorkspaceEditor() {
  const { workspaceState, updateFile } = useWorkspace();
  const { files, selectedFile } = workspaceState;

  const [editorCopyLabel, setEditorCopyLabel] = useState('Copy');

  const currentContent = selectedFile ? files[selectedFile] ?? '' : '';

  function handleEditorChange(value: string | undefined) {
    if (selectedFile && value !== undefined) {
      updateFile(selectedFile, value);
    }
  }

  async function handleCopyEditor() {
    if (!selectedFile) return;
    await navigator.clipboard.writeText(files[selectedFile] ?? '');
    setEditorCopyLabel('Copied!');
    setTimeout(() => setEditorCopyLabel('Copy'), 2000);
  }

  const filePathClass = selectedFile
    ? 'text-xs text-gray-600 bg-gray-50 px-2 py-1 border border-gray-300 rounded'
    : 'text-xs text-gray-400 bg-gray-50 px-2 py-1 border border-gray-300 rounded';

  const hasFiles = Object.keys(files).length > 0;
  const shareInputValue = hasFiles
    ? 'sh -c "$(curl -fsSL https://jame.io/api/install/demo.sh)"'
    : 'Add files to generate install link';

  return (
    <div className="workspace-center lg:col-span-1 border-r-2 border-black">
      <div className="p-4 bg-white h-full flex flex-col">
        {/* Header */}
        <div className="flex justify-between items-center mb-2">
          <h3 className="text-lg font-black text-black">Context editor</h3>
          <div className="flex gap-2">
            <button
              onClick={handleCopyEditor}
              className="px-3 py-1 border-2 border-black text-black font-bold shadow-[2px_2px_0px_0px_rgba(0,0,0,1)] hover:shadow-[4px_4px_0px_0px_rgba(0,0,0,1)] hover:translate-x-[-1px] hover:translate-y-[-1px] active:shadow-[1px_1px_0px_0px_rgba(0,0,0,1)] active:translate-x-[1px] active:translate-y-[1px] transition-all text-xs"
              style={{ backgroundColor: '#a7cefd' }}
            >
              {editorCopyLabel}
            </button>
          </div>
        </div>

        {/* File path */}
        <div className="mb-4">
          <div className={filePathClass} style={{ fontFamily: "'Courier New', monospace" }}>
            {selectedFile || 'No file selected'}
          </div>
        </div>

        {/* Editor */}
        <div className="relative flex-1 mb-3">
          <div className="w-full h-full bg-gray-900 translate-y-1 translate-x-1 absolute inset-0 z-0" />
          <div className="relative z-10 border-2 border-black bg-white h-full" style={{ minHeight: 320 }}>
            <Editor
              key={selectedFile ?? '__empty__'}
              height="100%"
              language={getLanguage(selectedFile)}
              value={currentContent}
              onChange={handleEditorChange}
              theme="vs"
              options={{
                fontSize: 12,
                lineNumbers: 'on',
                minimap: { enabled: false },
                scrollBeyondLastLine: false,
                automaticLayout: true,
                wordWrap: 'on',
              }}
            />
          </div>
        </div>

        {/* Share panel */}
        <div className="relative w-full">
          <div className="flex gap-0">
            <button className="share-tab-button active px-3 py-1 border-2 border-black border-b-0 text-black font-bold text-xs shadow-[2px_0px_0px_0px_rgba(0,0,0,1)] relative z-10" style={{ backgroundColor: '#FFC480' }}>
              macOS/Linux
            </button>
            <button
              // disabled
              className="share-tab-button px-3 py-1 bg-gray-100 border-2 border-gray-400 border-b-0 text-gray-500 font-medium text-xs shadow-[1px_0px_0px_0px_rgba(0,0,0,0.3)] relative z-5 cursor-not-allowed opacity-50"
            >
              Windows (soon)
            </button>
          </div>
          <div
            className="border-2 border-black shadow-[3px_3px_0px_0px_rgba(0,0,0,1)] p-3 relative z-0 w-full"
            style={{ marginTop: -2, overflow: 'hidden', backgroundColor: '#FFC480' }}
          >
            <div className="flex gap-2" style={{ minWidth: 0 }}>
              <input
                type="text"
                readOnly
                value={shareInputValue}
                className={`share-link-input px-3 py-1 bg-white border-2 border-black text-black font-mono text-xs shadow-[2px_2px_0px_0px_rgba(0,0,0,1)] focus:outline-none ${!hasFiles ? 'opacity-50' : ''}`}
                style={{ cursor: 'text' }}
              />
              <button
                disabled={!hasFiles}
                onClick={async () => {
                  await navigator.clipboard.writeText(shareInputValue);
                }}
                className="flex-shrink-0 px-3 py-1 border-2 border-black text-black font-bold shadow-[2px_2px_0px_0px_rgba(0,0,0,1)] hover:shadow-[3px_3px_0px_0px_rgba(0,0,0,1)] hover:translate-x-[-1px] hover:translate-y-[-1px] active:shadow-[1px_1px_0px_0px_rgba(0,0,0,1)] active:translate-x-[1px] active:translate-y-[1px] transition-all text-xs disabled:opacity-50 disabled:cursor-not-allowed"
                style={{ backgroundColor: '#a7cefd' }}
              >
                Copy
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
