import { useState } from 'react';
import { FileSearch } from 'lucide-react';
import { Sidebar } from './Sidebar';
import { Topbar } from './Topbar';
import './AppShell.css';

interface AppShellProps {
  children: React.ReactNode;
  activePage: string;
  onNavigate: (page: string) => void;
  activeSession?: string;
  onSelectSession?: (id: string) => void;
  onNewPackage?: () => void;
}

export function AppShell({
  children,
  activePage,
  onNavigate,
  activeSession,
  onSelectSession,
  onNewPackage,
}: AppShellProps) {
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);

  return (
    <div className="shell" data-sidebar-collapsed={sidebarCollapsed} data-style="ide" data-page={activePage}>
      <Topbar
        onToggleSidebar={() => setSidebarCollapsed(c => !c)}
        sidebarCollapsed={sidebarCollapsed}
      />

      <div className="shell__body">
        <Sidebar
          collapsed={sidebarCollapsed}
          activePage={activePage}
          activeSession={activeSession}
          onNavigate={onNavigate}
          onSelectSession={onSelectSession}
          onNewPackage={onNewPackage}
        />

        <main
          className="shell__main"
          id="main-content"
          onDragOver={(e) => e.preventDefault()}
          onDrop={(e) => e.preventDefault()}
        >
          <div key={`${activePage}:${activeSession ?? ''}`} className="page-transition">
            {children}
          </div>
        </main>
      </div>
    </div>
  );
}
