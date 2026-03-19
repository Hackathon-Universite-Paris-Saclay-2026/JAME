import { BrowserRouter as Router, Routes, Route } from 'react-router-dom';
import { WorkspaceProvider } from './context/WorkspaceContext';
import HomePage from './pages/HomePage';
import WorkspacePage from './pages/WorkspacePage';

export default function App() {
  return (
    <WorkspaceProvider>
      <Router>
        <Routes>
          <Route path="/" element={<HomePage />} />
          <Route path="/workspace" element={<WorkspacePage />} />
        </Routes>
      </Router>
    </WorkspaceProvider>
  );
}
