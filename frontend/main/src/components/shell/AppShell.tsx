import { useState } from 'react';

import { SidebarProvider } from '@/components/ui/sidebar';
import { AppSidebar } from './app-sidebar';
import { AppTopbar, type Crumb } from './app-topbar';
import { ShellSlotsContext } from './shellSlots';
import type { Page } from '../../app/route';
import type { Theme } from '../../app/theme';
import './AppShell.css';

interface AppShellProps {
  children: React.ReactNode;
  title: string;
  crumbs: Crumb[];
  activePage: Page;
  activePackage: string | null;
  theme: Theme;
  sidebarRefreshKey: number;
  /** The open review's newest "need you" count, so the sidebar never shows a stale one for it. */
  liveNeedYou: { packageId: string; count: number } | null;
  /** The evidence for the finding the reviewer opened, or nothing. Never open on its own. */
  evidencePanel?: React.ReactNode;
  onNavigate: (page: Page) => void;
  onOpenPackage: (packageId: string) => void;
  onNewReview: () => void;
  onToggleTheme: () => void;
}

/** The same key the sidebar used before #1034, so a reviewer's choice survives the change. */
export const COLLAPSED_KEY = 'gv-sidebar-collapsed';

function readCollapsed(): boolean {
  try {
    return localStorage.getItem(COLLAPSED_KEY) === 'true';
  } catch {
    return false;
  }
}

/**
 * The frame every screen sits in: sidebar, header, the screen, and — only when a finding's evidence
 * has been opened — the evidence panel beside it (a full-screen sheet under 1100px).
 *
 * The sidebar is shadcn's (#1034): collapsible to icons, remembered on this device, and a sheet on
 * phones. The header's two slots are where a screen places its status and its primary action.
 */
export function AppShell({
  children,
  title,
  crumbs,
  activePage,
  activePackage,
  theme,
  sidebarRefreshKey,
  liveNeedYou,
  evidencePanel,
  onNavigate,
  onOpenPackage,
  onNewReview,
  onToggleTheme,
}: AppShellProps) {
  const [open, setOpen] = useState(() => !readCollapsed());
  const [titleSlot, setTitleSlot] = useState<HTMLDivElement | null>(null);
  const [actionsSlot, setActionsSlot] = useState<HTMLDivElement | null>(null);
  const evidenceOpen = Boolean(evidencePanel);

  function changeOpen(next: boolean) {
    setOpen(next);
    try {
      localStorage.setItem(COLLAPSED_KEY, String(!next));
    } catch {
      // Storage blocked: the choice lasts for this visit.
    }
  }

  return (
    <ShellSlotsContext.Provider value={{ title: titleSlot, actions: actionsSlot }}>
      <SidebarProvider
        open={open}
        onOpenChange={changeOpen}
        className="shell h-dvh min-h-0"
        data-evidence-open={evidenceOpen}
        data-page={activePage}
        style={{ '--sidebar-width': '16.25rem' } as React.CSSProperties}
      >
        <a className="shell__skip" href="#main-content">Skip to content</a>

        <AppSidebar
          activePage={activePage}
          activePackage={activePackage}
          theme={theme}
          refreshKey={sidebarRefreshKey}
          liveNeedYou={liveNeedYou}
          onNavigate={onNavigate}
          onOpenPackage={onOpenPackage}
          onNewReview={onNewReview}
          onToggleTheme={onToggleTheme}
        />

        {/* Not shadcn's SidebarInset, which is a <main>: the screen below already is the page's main. */}
        <div className="shell__content flex min-w-0 flex-1 flex-col">
          <AppTopbar title={title} crumbs={crumbs} titleRef={setTitleSlot} actionsRef={setActionsSlot} />

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

            {evidenceOpen && (
              <aside className="shell__evidence" aria-label="Evidence">
                {evidencePanel}
              </aside>
            )}
          </div>
        </div>
      </SidebarProvider>
    </ShellSlotsContext.Provider>
  );
}
