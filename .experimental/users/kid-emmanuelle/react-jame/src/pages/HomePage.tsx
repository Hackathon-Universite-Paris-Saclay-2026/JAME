import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useWorkspace } from '../context/WorkspaceContext';
import Navbar from '../components/Navbar';
import Hero from '../components/Hero';
import Footer from '../components/Footer';
import QuickActions from '../components/QuickActions';
import FileModal from '../components/FileModal';
import ContextModal from '../components/ContextModal';

export default function HomePage() {
  const navigate = useNavigate();
  const [fileModalOpen, setFileModalOpen] = useState(false);
  const [fileModalBasePath, setFileModalBasePath] = useState('');
  const [contextModalOpen, setContextModalOpen] = useState(false);

  const { includeFile, createContext, switchContext } = useWorkspace();

  function handleShowWorkspace() {
    navigate('/workspace');
  }

  function handleOpenFileModal(basePath = '') {
    setFileModalBasePath(basePath);
    setFileModalOpen(true);
  }

  function handleFileModalSubmit(path: string) {
    const parts = path.split('/');
    const fileName = parts[parts.length - 1];
    const ext = fileName.split('.').pop()?.toLowerCase();

    let defaultContent = '# New file\n\nContent goes here...';
    if (ext === 'md') {
      defaultContent = `# ${fileName.replace('.md', '').replace(/[_-]/g, ' ')}\n\nYour content here...`;
    } else if (ext === 'json') {
      defaultContent = '{\n  "name": "new-file",\n  "version": "1.0.0"\n}';
    } else if (ext === 'yaml' || ext === 'yml') {
      defaultContent = 'name: new-file\nversion: 1.0.0\n';
    }

    includeFile(path, defaultContent);
    setFileModalOpen(false);
  }

  function handleContextModalSubmit(name: string) {
    const id = name.toLowerCase().replace(/[^a-z0-9]/g, '-');
    if (createContext(id, name)) {
      switchContext(id);
    }
    setContextModalOpen(false);
  }

  return (
    <div className="min-h-screen flex flex-col">
      <Navbar />
      <main className="flex-1 max-w-none mx-auto w-full px-4 py-20">
        <Hero />
        <QuickActions
          onShowWorkspace={handleShowWorkspace}
          onHideWorkspace={() => {}}
          isWorkspaceVisible={false}
        />
      </main>
      <Footer />

      <FileModal
        isOpen={fileModalOpen}
        basePath={fileModalBasePath}
        onSubmit={handleFileModalSubmit}
        onClose={() => setFileModalOpen(false)}
      />
      <ContextModal
        isOpen={contextModalOpen}
        onSubmit={handleContextModalSubmit}
        onClose={() => setContextModalOpen(false)}
      />
    </div>
  );
}
