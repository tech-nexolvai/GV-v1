import { useState } from 'react';
import { useMediaQuery } from '../../app/useMediaQuery';
import { ModalSheet } from './ModalSheet';
import { Sidebar } from './Sidebar';
import { Topbar } from './Topbar';
import { ShellSlotsContext } from './shellSlots';
import type { Page } from '../../app/route';
import type { Theme } from '../../app/theme';
import './AppShell.css';

interface AppShellProps {
  children: React.ReactNode;
  title: string;
  activePage: Page;
  activePackage: string | null;
  theme: Theme;
  sidebarRefreshKey: number;
  /** The evidence for the finding the reviewer opened, or nothing. Never open on its own. */
  evidencePanel?: React.ReactNode;
  onCloseEvidence: () => void;
  onNavigate: (page: Page) => void;
  onOpenPackage: (packageId: string) => void;
  onNewReview: () => void;
  onToggleTheme: () => void;
}

const COLLAPSED_KEY = 'gv-sidebar-collapsed';

function readCollapsed(): boolean {
  try {
    return localStorage.getItem(COLLAPSED_KEY) === 'true';
  } catch {
    return false;
  }
}

/**
 * The frame every screen sits in: sidebar, header, the screen, and — only when a finding's evidence
 * has been opened — the evidence panel beside it.
 *
 * The previous frame pinned an evidence column open on every review screen, the start screen
 * included, and squeezed the conversation into 360–480px; the findings table could not fit and the
 * "View evidence" button was pushed off the edge. The conversation now gets the room, and the
 * evidence takes its share only when there is evidence to show (a full-screen sheet under 1100px).
 */
export function AppShell({
  children,
  title,
  activePage,
  activePackage,
  theme,
  sidebarRefreshKey,
  evidencePanel,
  onCloseEvidence,
  onNavigate,
  onOpenPackage,
  onNewReview,
  onToggleTheme,
}: AppShellProps) {
  const [collapsed, setCollapsed] = useState(readCollapsed);
  const [mobileOpen, setMobileOpen] = useState(false);
  const [titleSlot, setTitleSlot] = useState<HTMLDivElement | null>(null);
  const [actionsSlot, setActionsSlot] = useState<HTMLDivElement | null>(null);
  const evidenceOpen = Boolean(evidencePanel);
  const mobileNavigation = useMediaQuery('(max-width: 767px)');
  const modalEvidence = useMediaQuery('(max-width: 1439px)');

  function toggleCollapsed() {
    setCollapsed((current) => {
      const next = !current;
      try {
        localStorage.setItem(COLLAPSED_KEY, String(next));
      } catch {
        // Storage blocked: the choice lasts for this visit.
      }
      return next;
    });
  }

  // Anything chosen from the drawer also closes it, so the reviewer lands on what they picked.
  function closingDrawer<A extends unknown[]>(action: (...args: A) => void) {
    return (...args: A) => {
      setMobileOpen(false);
      action(...args);
    };
  }

  const sidebar = <Sidebar
          collapsed={mobileNavigation ? false : collapsed}
          mobileOpen={mobileOpen}
          activePage={activePage}
          activePackage={activePackage}
          theme={theme}
          refreshKey={sidebarRefreshKey}
          onToggleCollapsed={toggleCollapsed}
          onCloseMobile={() => setMobileOpen(false)}
          onNavigate={closingDrawer(onNavigate)}
          onOpenPackage={closingDrawer(onOpenPackage)}
          onNewReview={closingDrawer(onNewReview)}
          onToggleTheme={onToggleTheme}
        />;

  return (
    <ShellSlotsContext.Provider value={{ title: titleSlot, actions: actionsSlot }}>
      <div className="shell" data-evidence-open={evidenceOpen} data-page={activePage}>
        <a className="shell__skip" href="#main-content" onClick={(event) => {
          event.preventDefault();
          document.getElementById('main-content')?.focus();
        }}>Skip to content</a>
        {mobileNavigation ? (
          <ModalSheet open={mobileOpen} onClose={() => setMobileOpen(false)} title="Navigation" className="shell__navigation-dialog">
            {sidebar}
          </ModalSheet>
        ) : sidebar}

        <div className="shell__content">
          <Topbar
            title={title}
            onOpenMenu={() => setMobileOpen(true)}
            titleRef={setTitleSlot}
            actionsRef={setActionsSlot}
          />

          <div className="shell__body">
            <main className="shell__main" id="main-content" tabIndex={-1}>
              {/* Keyed on the page and the package, so the fade replays on every navigation. */}
              <div key={`${activePage}:${activePackage ?? ''}`} className="page-transition">
                {children}
              </div>
              {/* The build partner, bottom-right and quiet, as the client's brief asks. */}
              <p className="shell__partner">
                built with <strong>Nexolv</strong>
              </p>
            </main>

            {evidenceOpen && !modalEvidence && (
              <aside className="shell__evidence" aria-label="Evidence">
                {evidencePanel}
              </aside>
            )}
          </div>
        </div>
      </div>
      {modalEvidence && (
        <ModalSheet open={evidenceOpen} onClose={onCloseEvidence} title="Drawing evidence and recorded facts" className="shell__evidence-dialog">
          {evidencePanel}
        </ModalSheet>
      )}
    </ShellSlotsContext.Provider>
  );
}
