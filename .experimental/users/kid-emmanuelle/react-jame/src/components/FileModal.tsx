import { useState, useEffect, useRef } from 'react';

interface FileModalProps {
  isOpen: boolean;
  basePath?: string;
  onSubmit: (path: string) => void;
  onClose: () => void;
}

export default function FileModal({ isOpen, basePath = '', onSubmit, onClose }: FileModalProps) {
  const [value, setValue] = useState('');
  const [error, setError] = useState('');
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (isOpen) {
      const initial = basePath ? (basePath.endsWith('/') ? basePath : basePath + '/') : '';
      setValue(initial);
      setError('');
      setTimeout(() => {
        inputRef.current?.focus();
        const len = inputRef.current?.value.length ?? 0;
        inputRef.current?.setSelectionRange(len, len);
      }, 50);
    }
  }, [isOpen, basePath]);

  function handleSubmit() {
    const path = value.trim();
    if (!path) {
      setError('Please enter a file path');
      return;
    }
    if (/[<>:"|?*]/.test(path)) {
      setError('File path contains invalid characters');
      return;
    }
    if (path.endsWith('/')) {
      setError('File path cannot end with /');
      return;
    }
    if (path.includes('//')) {
      setError('File path contains empty segments');
      return;
    }
    onSubmit(path);
  }

  function handleKeyDown(e: React.KeyboardEvent) {
    if (e.key === 'Enter') {
      e.preventDefault();
      handleSubmit();
    } else if (e.key === 'Escape') {
      e.preventDefault();
      onClose();
    }
  }

  if (!isOpen) return null;

  return (
    <div
      className="fixed inset-0 bg-black bg-opacity-50 z-50"
      onClick={onClose}
    >
      <div className="flex items-center justify-center min-h-screen p-4">
        <div
          className="bg-white border-2 border-black shadow-[8px_8px_0px_0px_rgba(0,0,0,1)] p-6 max-w-md w-full"
          onClick={(e) => e.stopPropagation()}
        >
          <h2 className="text-xl font-black text-black mb-4">Create file</h2>
          <div className="mb-6">
            <label className="block text-sm font-bold text-black mb-2">File path</label>
            <input
              ref={inputRef}
              type="text"
              value={value}
              onChange={(e) => setValue(e.target.value)}
              onKeyDown={handleKeyDown}
              className="w-full px-3 py-2 border-2 border-black focus:outline-none"
              placeholder="e.g. truc/machin/bobo.py"
              autoComplete="off"
            />
            {error && <div className="text-red-600 text-sm mt-2">{error}</div>}
          </div>
          <div className="flex gap-2 justify-end">
            <button
              onClick={onClose}
              className="px-4 py-2 bg-gray-200 border-2 border-black text-black font-bold shadow-[2px_2px_0px_0px_rgba(0,0,0,1)] hover:shadow-[3px_3px_0px_0px_rgba(0,0,0,1)] hover:translate-x-[-1px] hover:translate-y-[-1px] active:shadow-[1px_1px_0px_0px_rgba(0,0,0,1)] active:translate-x-[1px] active:translate-y-[1px] transition-all"
            >
              Cancel
            </button>
            <button
              onClick={handleSubmit}
              className="px-4 py-2 bg-cyan-400 border-2 border-black text-black font-bold shadow-[2px_2px_0px_0px_rgba(0,0,0,1)] hover:shadow-[3px_3px_0px_0px_rgba(0,0,0,1)] hover:translate-x-[-1px] hover:translate-y-[-1px] active:shadow-[1px_1px_0px_0px_rgba(0,0,0,1)] active:translate-x-[1px] active:translate-y-[1px] transition-all"
            >
              Create
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
