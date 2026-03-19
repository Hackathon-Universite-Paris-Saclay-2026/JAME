import { useState } from 'react';
import WorkspaceFiles from './WorkspaceFiles';
import WorkspaceEditor from './WorkspaceEditor';
import WorkspaceActions from './WorkspaceActions';

interface WorkspaceProps {
  isVisible: boolean;
  onOpenFileModal: (basePath?: string) => void;
  onOpenContextModal: () => void;
}

export default function Workspace({ isVisible, onOpenFileModal, onOpenContextModal }: WorkspaceProps) {
  const [recommendations, setRecommendations] = useState<{
    rules: string[];
    agents: string[];
    mcps: string[];
  } | null>(null);

  const isFullPage = !isVisible; // When isVisible is false, it means we're on the full-page workspace view

  return (
    <div className={isFullPage ? 'h-full w-full flex flex-col' : 'workspace-container'}>
      {/* Recommendations Banner */}
      {recommendations && (
        <div className="mb-4 bg-cyan-50 border-2 border-black shadow-[4px_4px_0px_0px_rgba(0,0,0,1)] p-4">
          <div className="flex justify-between items-start">
            <div className="flex-1">
              <h3 className="text-sm font-black mb-2 flex items-center gap-2">
                <span>✨</span> AI Recommended Tools for Your Repository
              </h3>
              <div className="text-sm flex flex-wrap gap-4">
                {recommendations.rules.length > 0 && (
                  <div>
                    <span className="font-bold">Rules:</span> {recommendations.rules.join(', ')}
                  </div>
                )}
                {recommendations.agents.length > 0 && (
                  <div>
                    <span className="font-bold">Agents:</span> {recommendations.agents.join(', ')}
                  </div>
                )}
                {recommendations.mcps.length > 0 && (
                  <div>
                    <span className="font-bold">MCPs:</span> {recommendations.mcps.join(', ')}
                  </div>
                )}
              </div>
            </div>
            <button
              onClick={() => setRecommendations(null)}
              className="ml-4 w-6 h-6 flex items-center justify-center bg-white border-2 border-black text-black font-bold hover:bg-gray-100 transition-colors"
              title="Close"
            >
              ×
            </button>
          </div>
        </div>
      )}

      <div className={isFullPage ? 'w-full flex-1 flex flex-col overflow-hidden' : 'workspace-inner'}>
        {/* Editor Container */}
        <div
          className="bg-white border-2 border-black shadow-[4px_4px_0px_0px_rgba(0,0,0,1)] relative z-0 flex-1"
          style={isFullPage ? { height: 'auto' } : { height: 600 }}
        >
          <div className="workspace-grid h-full" style={isFullPage ? {} : { height: '100%' }}>
            <WorkspaceFiles
              onOpenFileModal={onOpenFileModal}
              onOpenContextModal={onOpenContextModal}
            />
            <WorkspaceEditor />
            <WorkspaceActions />
          </div>
        </div>
      </div>
    </div>
  );
}
