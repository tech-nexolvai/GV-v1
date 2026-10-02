import { useEffect, useState } from 'react';
import { AppShell } from './components/shell/AppShell';
import { ReviewPage } from './pages/ReviewPage';
import { PackagesPage } from './pages/PackagesPage';
import { WelcomePage } from './pages/WelcomePage';
import { CompanySettingsPage } from './pages/CompanySettingsPage';
import { RulebookPage } from './pages/RulebookPage';
import { UsagePage } from './pages/UsagePage';
import { useRoute } from './app/route';
import type { Page } from './app/route';
import { useTheme } from './app/theme';
import { useDocumentPosition } from './app/useDocumentPosition';
import './design/components.css';

const PAGE_TITLES: Record<Page, string> = {
  review: 'New review',
  documents: 'Documents',
  rulebook: 'Rulebook',
  settings: 'Company settings',
  usage: 'Usage',
};

export default function App() {
  const [route, navigate] = useRoute();
  const [theme, toggleTheme] = useTheme();
  const [documentPosition, rememberDocumentPosition] = useDocumentPosition();
  // Evidence belongs to the package that requested it, including during browser Back/Forward.
  const [evidencePanel, setEvidencePanel] = useState<{ route: typeof route; packageId: string; panel: React.ReactNode } | null>(null);
  const [evidenceDismissKey, setEvidenceDismissKey] = useState(0);
  // The vendor of the review on screen, reported by the review once its package has loaded.
  const [reviewTitle, setReviewTitle] = useState<{ packageId: string; title: string } | null>(null);
  // Refresh after confirmed creation/approval; package status remains server-owned.
  const [sidebarRefreshKey, setSidebarRefreshKey] = useState(0);

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

  return (
    <AppShell
      title={title}
      activePage={route.page}
      activePackage={packageId}
      theme={theme}
      sidebarRefreshKey={sidebarRefreshKey}
      evidencePanel={evidencePanel?.route === route && evidencePanel.packageId === packageId ? evidencePanel.panel : null}
      onCloseEvidence={() => { setEvidencePanel(null); setEvidenceDismissKey((key) => key + 1); }}
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
          evidenceDismissKey={evidenceDismissKey}
          onEvidenceChange={(panel) => setEvidencePanel(panel ? { route, packageId, panel } : null)}
          onTitleChange={(vendor) => setReviewTitle({ packageId, title: vendor })}
          onPackageChanged={() => setSidebarRefreshKey((key) => key + 1)}
          onBackToDocuments={() => go('documents')}
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
