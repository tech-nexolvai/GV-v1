import { useState } from 'react';
import { Sidebar } from './Sidebar';
import { Topbar } from './Topbar';
import './AppShell.css';

interface AppShellProps {
  children: React.ReactNode;
  activePage: string;
  onNavigate: (page: string) => void;
  activeSession?: string;
  onSelectSession?: (id: string) => void;
  evidencePanel?: React.ReactNode;
  onNewPackage?: () => void;
}

export function AppShell({
  children,
  activePage,
  onNavigate,
  activeSession,
  onSelectSession,
  evidencePanel,
  onNewPackage,
}: AppShellProps) {
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const showEvidencePanel = Boolean(evidencePanel);

  return (
    <div className="shell" data-sidebar-collapsed={sidebarCollapsed}>
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
          {children}
        </main>

        {/* Evidence panel — slides in from right */}
        <div
          className={`shell__evidence ${showEvidencePanel ? 'shell__evidence--open' : ''}`}
          aria-label="Evidence viewer"
        >
          {evidencePanel || (
            <div
              className="evidence-panel-placeholder"
              style={{
                padding: 'var(--space-8)',
                color: 'var(--text-muted)',
                fontSize: 'var(--text-sm)',
                textAlign: 'center',
                height: '100%',
                display: 'flex',
                flexDirection: 'column',
                alignItems: 'center',
                justifyContent: 'center',
                gap: 'var(--space-3)'
              }}
            >
              <span>Select a finding, then open its recorded evidence crop.</span>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
