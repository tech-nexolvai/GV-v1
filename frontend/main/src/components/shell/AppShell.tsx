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
  const [mainWidth, setMainWidth] = useState<number | null>(null);
  const showEvidencePanel = Boolean(evidencePanel);

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
          style={{ width: mainWidth ? `${mainWidth}px` : undefined, flex: mainWidth ? 'none' : undefined }}
          onDragOver={(e) => e.preventDefault()}
          onDrop={(e) => e.preventDefault()}
        >
          {/* Keyed on the page *and the selected package*, so React discards the wrapper and the
              entrance replays on every navigation. Without a key at all the class stays mounted,
              the animation runs once on first paint, and every later change is an instant swap.

              The session has to be in the key too. Welcome and Review are both `activePage
              === 'review'` — they are told apart by whether a package is selected — so keying on
              the page alone left the single most-used transition in the product, opening a document
              set from the welcome screen, as the one navigation with no animation at all. */}
          <div key={`${activePage}:${activeSession ?? ''}`} className="page-transition">
            {children}
          </div>
        </main>

        <div 
          className="shell__resizer" 
          onMouseDown={(e) => {
            const startX = e.clientX;
            const startWidth = mainWidth || 480;
            const onMouseMove = (moveEvent: MouseEvent) => {
              const delta = startX - moveEvent.clientX; // Left side gets wider if dragging left, so right pane (main) gets wider if delta is positive
              setMainWidth(Math.max(360, Math.min(800, startWidth + delta)));
            };
            const onMouseUp = () => {
              document.removeEventListener('mousemove', onMouseMove);
              document.removeEventListener('mouseup', onMouseUp);
            };
            document.addEventListener('mousemove', onMouseMove);
            document.addEventListener('mouseup', onMouseUp);
          }}
        />

        {/* Evidence panel — slides in from right */}
        <div
          className={`shell__evidence ${showEvidencePanel ? 'shell__evidence--open' : ''}`}
          aria-label="Evidence viewer"
        >
          {evidencePanel || (
            <div className="evidence-placeholder">
              <FileSearch size={22} aria-hidden="true" />
              <span>Open a finding's evidence to see the recorded crop here.</span>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
