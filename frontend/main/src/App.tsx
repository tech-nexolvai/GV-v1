import { lazy, Suspense, useCallback, useEffect, useState } from 'react';
import { AppShell } from './components/shell/AppShell';
import type { Crumb } from './components/shell/app-topbar';
import { ReviewPage } from './pages/ReviewPage';
import { PackagesPage } from './pages/PackagesPage';
import { WelcomePage } from './pages/WelcomePage';
import { CompanySettingsPage } from './pages/CompanySettingsPage';
import { RulebookPage } from './pages/RulebookPage';
import { UsagePage } from './pages/UsagePage';
import { useRoute } from './app/route';
import type { Page } from './app/route';
import { useTheme } from './app/theme';
import { projectId } from './api/config';
import { readDocumentPosition, saveDocumentPosition } from './pages/documentPosition';
import './design/components.css';

// Loaded on demand: the kit pulls in charts and tables no reviewer screen needs yet.
const UiKitPage = lazy(() => import('./pages/ui-kit/UiKitPage'));

const PAGE_TITLES: Record<Page, string> = {
  review: 'New review',
  documents: 'Documents',
  rulebook: 'Rulebook',
  settings: 'Company settings',
  usage: 'Usage',
  'ui-kit': 'UI kit',
};

export default function App() {
  const [route, navigate] = useRoute();
  const [theme, toggleTheme] = useTheme();
  const [evidencePanel, setEvidencePanel] = useState<React.ReactNode>(null);
  // The vendor and revision of the review on screen, reported by the review once its package has loaded.
  const [reviewTitle, setReviewTitle] = useState<{ packageId: string; title: string; revision: number | null } | null>(null);
  // The open review's newest "need you" count, so the sidebar's copy of it is never stale.
  const [liveNeedYou, setLiveNeedYou] = useState<{ packageId: string; count: number } | null>(null);
  // Bumped after a new review is created, so the sidebar lists it without a reload.
  const [sidebarRefreshKey, setSidebarRefreshKey] = useState(0);
  const [documentPosition, setDocumentPosition] = useState(() => {
    try { return readDocumentPosition(window.sessionStorage, projectId()); }
    catch { return readDocumentPosition(null, projectId()); }
  });
  const rememberDocumentPosition = useCallback((cursors: readonly string[]) => {
    let notice: string | null;
    try { notice = saveDocumentPosition(window.sessionStorage, projectId(), cursors); }
    catch { notice = saveDocumentPosition(null, projectId(), cursors); }
    setDocumentPosition({ cursors, notice });
  }, []);

  const packageId = route.page === 'review' ? route.packageId : null;
  const title =
    packageId !== null
      ? reviewTitle?.packageId === packageId
        ? reviewTitle.title
        : 'Review'
      : PAGE_TITLES[route.page];

  useEffect(() => {
    document.title = `${title} · GV Review`;
  }, [title]);

  // Where you are: Documents › vendor › Revision N on a review, the page's own name elsewhere.
  const revision = packageId !== null && reviewTitle?.packageId === packageId ? reviewTitle.revision : null;
  const crumbs: Crumb[] =
    packageId !== null
      ? [
          { label: 'Documents', onSelect: () => go('documents') },
          { label: title },
          ...(revision !== null ? [{ label: `Revision ${revision}` }] : []),
        ]
      : [{ label: title }];

  function go(page: Page) {
    setEvidencePanel(null);
    navigate({ page, packageId: null });
  }

  function openReview(id: string) {
    setEvidencePanel(null);
    navigate({ page: 'review', packageId: id });
  }

  function newReview() {
    setEvidencePanel(null);
    navigate({ page: 'review', packageId: null });
  }

  // The component reference renders on its own, outside the legacy shell.
  if (route.page === 'ui-kit') {
    return (
      <Suspense fallback={null}>
        <UiKitPage />
      </Suspense>
    );
  }

  return (
    <AppShell
      title={title}
      crumbs={crumbs}
      liveNeedYou={liveNeedYou}
      activePage={route.page}
      activePackage={packageId}
      theme={theme}
      sidebarRefreshKey={sidebarRefreshKey}
      evidencePanel={evidencePanel}
      onNavigate={go}
      onOpenPackage={openReview}
      onNewReview={newReview}
      onToggleTheme={toggleTheme}
    >
      {route.page === 'review' && packageId === null && (
        <WelcomePage
          onCreated={(id) => {
            setSidebarRefreshKey((key) => key + 1);
            openReview(id);
          }}
          onOpenReview={openReview}
        />
      )}

      {route.page === 'review' && packageId !== null && (
        <ReviewPage
          key={packageId}
          sessionId={packageId}
          onEvidenceChange={setEvidencePanel}
          onTitleChange={(vendor, revisionNumber) => setReviewTitle({ packageId, title: vendor, revision: revisionNumber })}
          onNeedYouChange={(count) => setLiveNeedYou(count === null ? null : { packageId, count })}
          onBackToDocuments={() => go('documents')}
          onPackageChanged={() => setSidebarRefreshKey((key) => key + 1)}
        />
      )}

      {route.page === 'documents' && (
        <PackagesPage onOpenReview={openReview} onNewPackage={newReview}
          initialCursors={documentPosition.cursors} positionNotice={documentPosition.notice}
          onPositionChange={rememberDocumentPosition} />
      )}

      {route.page === 'rulebook' && <RulebookPage />}

      {route.page === 'settings' && <CompanySettingsPage />}

      {route.page === 'usage' && <UsagePage />}
    </AppShell>
  );
}
