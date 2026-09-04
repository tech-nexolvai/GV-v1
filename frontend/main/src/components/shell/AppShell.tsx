import { useState } from 'react';
import { Sidebar } from './Sidebar';
import { Topbar } from './Topbar';
import { FileText } from 'lucide-react';
import './AppShell.css';

interface AppShellProps {
  children: React.ReactNode;
  activePage: string;
  onNavigate: (page: string) => void;
  activeSession?: string;
  onSelectSession?: (id: string) => void;
  evidencePanel?: React.ReactNode;
  onNewPackage?: () => void;
  designStyle: 'stone' | 'ide';
  onStyleChange: (style: 'stone' | 'ide') => void;
}

export function AppShell({
  children,
  activePage,
  onNavigate,
  activeSession,
  onSelectSession,
  evidencePanel,
  onNewPackage,
  designStyle,
  onStyleChange,
}: AppShellProps) {
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const showEvidencePanel = Boolean(evidencePanel);

  return (
    <div className="shell" data-sidebar-collapsed={sidebarCollapsed}>
      <Topbar
        onToggleSidebar={() => setSidebarCollapsed(c => !c)}
        sidebarCollapsed={sidebarCollapsed}
        designStyle={designStyle}
        onStyleChange={onStyleChange}
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
          onDrop={(e) => {
            const data = e.dataTransfer.getData('text/plain');
            if (data === 'evidence-panel') {
              onStyleChange(designStyle === 'stone' ? 'ide' : 'stone');
            }
          }}
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
              draggable
              onDragStart={(e) => {
                e.dataTransfer.setData('text/plain', 'evidence-panel');
              }}
              style={{
                cursor: 'grab',
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
              title="Drag drawings panel to swap column positions"
            >
              <FileText size={28} className="text-muted" style={{ opacity: 0.6 }} />
              <span>Select a check card to load drawing evidence</span>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
