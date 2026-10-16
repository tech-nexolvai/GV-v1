import { lazy, Suspense, useCallback, useEffect, useState } from 'react';
import { AppShell } from './components/shell/AppShell';
import type { Crumb } from './components/shell/app-topbar';
import { ReviewPage } from './pages/ReviewPage';
import { SimpleReviewPage } from './pages/SimpleReviewPage';
import { AdvancedBanner } from './components/simple-review/advanced-banner';
import { PackagesPage } from './pages/PackagesPage';
import { WelcomePage } from './pages/WelcomePage';
import { CompanySettingsPage } from './pages/CompanySettingsPage';
import { RulebookPage } from './pages/RulebookPage';
import { UsagePage } from './pages/UsagePage';
import { ChatPage } from './pages/ChatPage';
import { useRoute } from './app/route';
import type { Page } from './app/route';
import { useTheme } from './app/theme';
import { projectId } from './api/config';
import { readDocumentPosition, saveDocumentPosition } from './pages/documentPosition';
import './design/components.css';

// Loaded on demand: the kit pulls in charts and tables no reviewer screen needs yet.
const UiKitPage = lazy(() => import('./pages/ui-kit/UiKitPage'));

const PAGE_TITLES: Record<Page, string> = {
  review: 'Start a review',
  chat: 'Chat',
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
  // The drawing set whose chats are open, named by the chat page once its chat has loaded (#1464).
  const [chatName, setChatName] = useState<{ packageId: string; name: string } | null>(null);
  // The open review's newest open count (shown as "N optional"), so the sidebar's copy is never stale.
  const [liveNeedYou, setLiveNeedYou] = useState<{ packageId: string; count: number } | null>(null);
  // Whether the open review's failed reading is restarting now (`reading.retrying`, #1249), so the
  // sidebar's badge says the same as the review's header.
  const [liveRestarting, setLiveRestarting] = useState<{ packageId: string; restarting: boolean } | null>(null);
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
  // The drawing set whose chats are open (#1464); the sidebar marks it like an open review.
  const chatPackageId = route.page === 'chat' ? route.packageId : null;
  const chatSetName =
    chatPackageId !== null
      ? chatName?.packageId === chatPackageId
        ? chatName.name
        : reviewTitle?.packageId === chatPackageId
          ? reviewTitle.title
          : null
      : null;
  const title =
    packageId !== null
      ? reviewTitle?.packageId === packageId
        ? reviewTitle.title
        : 'Review'
      : chatPackageId !== null
        ? chatSetName ?? PAGE_TITLES.chat
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
      : chatPackageId !== null
        ? [{ label: 'Documents', onSelect: () => go('documents') }, { label: title }]
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

  /** A drawing set's chats (#1464): its newest chat, or a new one. Where an upload lands. */
  function openChats(id: string) {
    setEvidencePanel(null);
    navigate({ page: 'chat', packageId: id });
  }

  // Stable, so the chat page's effects that use them run only when the chat changes.
  const openChat = useCallback(
    (id: string | null, options?: { replace?: boolean }) => {
      // null: a new chat (`#/chat/<set>`), made on its first message.
      if (chatPackageId !== null) navigate(id ? { page: 'chat', packageId: chatPackageId, chatId: id } : { page: 'chat', packageId: chatPackageId }, options);
    },
    [navigate, chatPackageId],
  );
  const nameChat = useCallback((name: string) => {
    if (chatPackageId !== null) setChatName({ packageId: chatPackageId, name });
  }, [chatPackageId]);

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
      liveRestarting={packageId !== null && liveRestarting?.packageId === packageId ? liveRestarting : null}
      activePage={route.page}
      activePackage={packageId ?? chatPackageId}
      theme={theme}
      sidebarRefreshKey={sidebarRefreshKey}
      evidencePanel={evidencePanel}
      onNavigate={go}
      onOpenPackage={openChats}
      onOpenReviewList={openReview}
      onNewReview={newReview}
      onToggleTheme={toggleTheme}
    >
      {route.page === 'review' && packageId === null && (
        <WelcomePage
          onCreated={(id) => {
            setSidebarRefreshKey((key) => key + 1);
            // Straight to the chat (#1464): the PDFs are there and the agent reads them itself.
            openChats(id);
          }}
          onOpenSaved={(id) => {
            setSidebarRefreshKey((key) => key + 1);
            openReview(id);
          }}
          onOpenReview={openReview}
        />
      )}

      {/* The simple review page (#1343) at #/review/<id>; the old tabs at #/review/<id>/advanced. */}
      {route.page === 'review' && packageId !== null && !route.advanced && (
        <SimpleReviewPage
          key={packageId}
          packageId={packageId}
          onOpenAdvanced={() => navigate({ page: 'review', packageId, advanced: true })}
          onOpenChat={() => openChats(packageId)}
          onTitleChange={(vendor, revisionNumber) => setReviewTitle({ packageId, title: vendor, revision: revisionNumber })}
          onNeedYouChange={(count) => setLiveNeedYou(count === null ? null : { packageId, count })}
          onPackageChanged={() => setSidebarRefreshKey((key) => key + 1)}
        />
      )}

      {route.page === 'review' && packageId !== null && route.advanced && (
        <AdvancedBanner onBack={() => navigate({ page: 'review', packageId })} />
      )}

      {route.page === 'review' && packageId !== null && route.advanced && (
        <ReviewPage
          key={packageId}
          sessionId={packageId}
          onEvidenceChange={setEvidencePanel}
          onTitleChange={(vendor, revisionNumber) => setReviewTitle({ packageId, title: vendor, revision: revisionNumber })}
          onNeedYouChange={(count) => setLiveNeedYou(count === null ? null : { packageId, count })}
          onRestartingChange={(restarting) => setLiveRestarting({ packageId, restarting })}
          onBackToDocuments={() => go('documents')}
          onPackageChanged={() => setSidebarRefreshKey((key) => key + 1)}
        />
      )}

      {route.page === 'chat' && chatPackageId !== null && (
        <ChatPage
          key={chatPackageId}
          packageId={chatPackageId}
          chatId={route.chatId ?? null}
          onOpenChat={openChat}
          onNameChange={nameChat}
        />
      )}

      {route.page === 'documents' && (
        <PackagesPage onOpenReview={openReview} onNewPackage={newReview}
          initialCursors={documentPosition.cursors} positionNotice={documentPosition.notice}
          onPositionChange={rememberDocumentPosition} />
      )}

      {route.page === 'rulebook' && <RulebookPage />}

      {route.page === 'settings' && <CompanySettingsPage focus={route.focus} project={route.project} />}

      {route.page === 'usage' && <UsagePage />}
    </AppShell>
  );
}
