import { lazy, Suspense, useCallback, useEffect, useState } from 'react';
import { AppShell } from './components/shell/AppShell';
import type { Crumb } from './components/shell/app-topbar';
import { PackagesPage } from './pages/PackagesPage';
import { WelcomePage } from './pages/WelcomePage';
import { CompanySettingsPage } from './pages/CompanySettingsPage';
import { RulebookPage } from './pages/RulebookPage';
import { UsagePage } from './pages/UsagePage';
import { TrackerPage } from './pages/TrackerPage';
import { ChatPage } from './pages/ChatPage';
import { Toaster } from './components/ui/sonner';
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
  tracker: 'Tracker',
  rulebook: 'Rulebook',
  settings: 'Company settings',
  usage: 'Usage',
  'ui-kit': 'UI kit',
};

export default function App() {
  const [route, navigate] = useRoute();
  const [theme, toggleTheme] = useTheme();
  // The drawing set whose chats are open, named by the chat page once its chat has loaded (#1464).
  const [chatName, setChatName] = useState<{ packageId: string; name: string } | null>(null);
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

  // The drawing set whose chats are open (#1464); the sidebar marks it as the open set.
  const chatPackageId = route.page === 'chat' ? route.packageId : null;
  const chatSetName = chatPackageId !== null && chatName?.packageId === chatPackageId ? chatName.name : null;
  const title = chatPackageId !== null ? chatSetName ?? PAGE_TITLES.chat : PAGE_TITLES[route.page];

  useEffect(() => {
    document.title = `${title} · GV Review`;
  }, [title]);

  // Where you are: Documents › the drawing set on a chat, the page's own name elsewhere.
  const crumbs: Crumb[] =
    chatPackageId !== null
      ? [{ label: 'Documents', onSelect: () => go('documents') }, { label: title }]
      : [{ label: title }];

  function go(page: Page) {
    navigate({ page, packageId: null });
  }

  function newReview() {
    navigate({ page: 'review', packageId: null });
  }

  /** A drawing set's chats (#1464): its newest chat, or a new one. Where an upload lands. */
  function openChats(id: string) {
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
      activePage={route.page}
      activePackage={chatPackageId}
      theme={theme}
      sidebarRefreshKey={sidebarRefreshKey}
      onNavigate={go}
      onOpenPackage={openChats}
      onNewReview={newReview}
      onToggleTheme={toggleTheme}
    >
      {route.page === 'review' && (
        <WelcomePage
          onCreated={(id) => {
            setSidebarRefreshKey((key) => key + 1);
            // Straight to the chat (#1464): the PDFs are there and the agent reads them itself.
            openChats(id);
          }}
          onOpenSaved={(id) => {
            setSidebarRefreshKey((key) => key + 1);
            openChats(id);
          }}
          // "Continue a review" opens the drawing set's chat (decided 2026-10-28, the old review screens retired).
          onOpenReview={openChats}
        />
      )}

      {route.page === 'chat' && chatPackageId !== null && (
        <ChatPage
          key={chatPackageId}
          packageId={chatPackageId}
          chatId={route.chatId ?? null}
          onOpenChat={openChat}
          onNameChange={nameChat}
          focusFinding={route.finding ?? null}
        />
      )}

      {route.page === 'documents' && (
        <PackagesPage
          onOpenSet={(id, chatId) => navigate(chatId ? { page: 'chat', packageId: id, chatId } : { page: 'chat', packageId: id })}
          onNewPackage={newReview}
          initialCursors={documentPosition.cursors} positionNotice={documentPosition.notice}
          onPositionChange={rememberDocumentPosition} />
      )}

      {route.page === 'tracker' && (
        <TrackerPage
          onOpenItem={(link) => navigate({ page: 'chat', packageId: link.package_id, chatId: link.chat_id, finding: { messageId: link.message_id, findingId: link.finding_id } })}
          onOpenSet={openChats}
        />
      )}

      {route.page === 'rulebook' && <RulebookPage />}

      {route.page === 'settings' && <CompanySettingsPage focus={route.focus} project={route.project} />}

      {route.page === 'usage' && <UsagePage />}
      {/* #1744: one place for short notes (stopping a check, a check done, back online), bottom right. */}
      <Toaster position="bottom-right" offset={24} mobileOffset={16} duration={4000} />
    </AppShell>
  );
}
