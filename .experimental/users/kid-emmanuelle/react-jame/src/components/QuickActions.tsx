import { useState, useRef, type DragEvent } from 'react';

interface QuickActionsProps {
  onShowWorkspace: (focusSection?: string) => void;
  onHideWorkspace: () => void;
  isWorkspaceVisible: boolean;
}

export default function QuickActions({ onShowWorkspace }: QuickActionsProps) {
  const [projectDetails, setProjectDetails] = useState('');
  const [dragOver, setDragOver] = useState(false);
  const [uploadedFile, setUploadedFile] = useState<File | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  function handleDragOver(e: DragEvent<HTMLDivElement>) {
    e.preventDefault();
    setDragOver(true);
  }

  function handleDragLeave() {
    setDragOver(false);
  }

  function handleDrop(e: DragEvent<HTMLDivElement>) {
    e.preventDefault();
    setDragOver(false);
    const file = e.dataTransfer.files[0];
    if (file && (file.type === 'image/png' || file.type === 'image/jpeg')) {
      setUploadedFile(file);
    }
  }

  function handleFileInput(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (file) setUploadedFile(file);
  }

  function handleGenerate() {
    if (!projectDetails.trim()) return;
    onShowWorkspace();
  }

  return (
    <div className="max-w-2xl mx-auto mb-8 relative">
      <div
        className="bg-yellow-50 border-4 border-black border-solid rounded-lg shadow-[6px_6px_0px_0px_rgba(0,0,0,1)] p-6"
        style={{
          backgroundImage: `
            url("data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 width=%22100%22 height=%22100%22%3E%3Cfilter id=%22noise%22%3E%3CfeTurbulence type=%22fractalNoise%22 baseFrequency=%220.9%22 numOctaves=%223%22/%3E%3C/filter%3E%3Crect width=%22100%22 height=%22100%22 fill=%22%23fffaf0%22 filter=%22url(%23noise)%22 opacity=%220.18%22/%3E%3C/svg%3E"),
            linear-gradient(#0000000a 1px, transparent 1px),
            linear-gradient(to right, #0000000a 1px, transparent 1px)
          `,
          backgroundSize: '100% 100%, 100% 2.5rem, 2.5rem 100%',
          backgroundBlendMode: 'overlay, normal, normal',
        }}
      >
        {/* Text Area */}
        <div className="mb-4">
          <label className="block text-sm font-black text-black mb-1 uppercase tracking-wide">
            Project Details
          </label>
          <textarea
            value={projectDetails}
            onChange={(e) => setProjectDetails(e.target.value)}
            placeholder="Describe your app in detail (e.g., A full-stack Todo list with user accounts)..."
            rows={4}
            className="w-full px-3 py-2 text-sm border-2 border-black shadow-[2px_2px_0px_0px_rgba(0,0,0,1)] focus:shadow-[3px_3px_0px_0px_rgba(0,0,0,1)] focus:translate-x-[-1px] focus:translate-y-[-1px] transition-all focus:outline-none resize-none"
          />
        </div>

        {/* Drag & Drop Zone */}
        <div
          onClick={() => fileInputRef.current?.click()}
          onDragOver={handleDragOver}
          onDragLeave={handleDragLeave}
          onDrop={handleDrop}
          className={`mb-5 border-2 border-dashed border-black p-6 text-center cursor-pointer transition-all ${
            dragOver ? 'bg-cyan-50 shadow-[3px_3px_0px_0px_rgba(0,0,0,1)] -translate-x-[1px] -translate-y-[1px]' : 'bg-gray-50 hover:bg-cyan-50'
          }`}
        >
          <input
            ref={fileInputRef}
            type="file"
            accept="image/png,image/jpeg"
            className="hidden"
            onChange={handleFileInput}
          />
          {uploadedFile ? (
            <div className="flex flex-col items-center gap-1">
              <span className="text-sm font-bold text-black">{uploadedFile.name}</span>
              <span className="text-xs text-gray-500">Click to replace</span>
            </div>
          ) : (
            <div className="flex flex-col items-center gap-1">
              <span className="text-sm font-black text-black">Drag & Drop App Sketch / Maquette</span>
              <span className="text-xs text-gray-500">PNG / JPG</span>
            </div>
          )}
        </div>

        {/* Generate Button */}
        <button
          onClick={handleGenerate}
          disabled={!projectDetails.trim()}
          className="w-full py-3 border-2 border-black text-black font-black text-base shadow-[4px_4px_0px_0px_rgba(0,0,0,1)] hover:shadow-[5px_5px_0px_0px_rgba(0,0,0,1)] hover:translate-x-[-1px] hover:translate-y-[-1px] active:shadow-[2px_2px_0px_0px_rgba(0,0,0,1)] active:translate-x-[1px] active:translate-y-[1px] transition-all disabled:opacity-40 disabled:cursor-not-allowed"
          style={{ backgroundColor: '#FFC480' }}
        >
          Generate Whole App
        </button>
      </div>
    </div>
  );
}
