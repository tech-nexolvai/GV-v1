/**
 * A drawing set's chats (#1464): where an upload lands, and what a drawing set in the sidebar opens.
 *
 * The owner's idea: "When the user uploads the drawing set, it redirects him to the chat. Per
 * drawing set we have many chats. The user asks; the agent has exactly Claude's tools, with efforts,
 * models, thinking." So this page is the product and looks and moves like claude.ai (#1471): one
 * sidebar (the set's chats are listed in the app's sidebar under the set's name), the conversation
 * in a centred column, the composer at the bottom. An empty chat holds the composer in the middle
 * under a greeting, and the first message glides it down. Switching chats fades the thread; every
 * animation stops under "reduce motion".
 *
 * `#/chat/<set>` opens the set's newest chat (or a new empty one) and replaces itself with
 * `#/chat/<set>/<chat>`, so Back does not bounce. A message streams in event by event; when it
 * ends, the stored message replaces what was built live, so a reload shows exactly the same thing.
 *
 * Read-only toward the review: an answer here is an AI answer ("check before acting") and never
 * becomes a recorded result. Leaving a chat while a message runs stops that message (the server
 * stops it when the stream closes, and saves it as stopped).
 */

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { ArrowDown, ChevronRight, CircleAlert, ListChecks, RotateCw, Sparkle } from 'lucide-react';

import { formatRoute } from '@/app/route';
import { HeaderActions, HeaderTitleExtra, SidebarChats } from '@/components/shell/ShellHeader';
import { Button } from '@/components/ui/button';
import { Skeleton } from '@/components/ui/skeleton';
import { TooltipProvider } from '@/components/ui/tooltip';
import { LiveSteps } from '@/components/assistant/assistant-steps';
import { ChatComposer } from '@/components/chat/chat-composer';
import { ChatList, type ChatListState } from '@/components/chat/chat-list';
import { AssistantMessage, UserMessage } from '@/components/chat/chat-message';
import { ApiError } from '@/api/client';
import { chatFailureWords, isAbort, LIVE_CHAT_API, type Chat, type ChatApi, type ChatModels } from '@/lib/chat-api';
import { applyChatEvent, CHAT_STARTERS, chatTitle, dollars, fromSaved, localAnswer, localQuestion, settle, type ViewMessage } from '@/lib/chat-messages';
import { browserStorage, readChoice, resolveChoice, saveChoice, type ChatChoice } from '@/lib/chat-settings';
import { cn } from '@/lib/utils';

/** A chat with nothing in it yet (one value, so effects that follow the messages do not re-run). */
const NO_MESSAGES: ViewMessage[] = [];

/** How close to the bottom (px) still counts as "at the bottom" for following new words. */
const FOLLOW_GAP = 80;
/** claude.ai's words under the composer. */
const DISCLAIMER = 'Claude can make mistakes. Please double-check responses.';

/** "$5.00" → "$5", "$3.10" stays: the money line in the model menu. */
function money(value: string | null | undefined): string | null {
  return dollars(value)?.replace(/\.00$/, '') ?? null;
}

type ModelsState = { status: 'loading' } | { status: 'error'; message: string } | { status: 'ready'; data: ChatModels };

/**
 * Opening `#/chat/<set>`: the newest chat, else a new one. One request per set at a time, so React's
 * double-run of effects in development never makes two empty chats.
 */
const opening = new Map<string, Promise<string>>();
function newestOrNewChat(api: ChatApi, packageId: string): Promise<string> {
  const known = opening.get(packageId);
  if (known) return known;
  const promise = api
    .list(packageId)
    .then(({ chats }) => chats[0]?.id ?? api.create(packageId).then((made) => made.id))
    .finally(() => opening.delete(packageId));
  opening.set(packageId, promise);
  return promise;
}

export interface ChatPageProps {
  packageId: string;
  /** The open chat; null on `#/chat/<set>` until the newest (or a new) chat is found. */
  chatId: string | null;
  /** Move to a chat. `replace` swaps the address without a new Back step. */
  onOpenChat: (chatId: string, options?: { replace?: boolean }) => void;
  /** The drawing set's name, once known, for the top bar and the tab title. */
  onNameChange?: (name: string) => void;
  api?: ChatApi;
  /** How long Stop waits for the server's stored (stopped) message before it stops reading. Tests pass 0. */
  stopGraceMs?: number;
  /** How often a chat answered elsewhere (another tab, before a reload) is read again until it is saved. */
  runningPollMs?: number;
}

export function ChatPage({ packageId, chatId, onOpenChat, onNameChange, api = LIVE_CHAT_API, stopGraceMs = 3000, runningPollMs = 3000 }: ChatPageProps) {
  // ── The models and the reviewer's choice ──
  const [models, setModels] = useState<ModelsState>({ status: 'loading' });
  const [modelsAsk, setModelsAsk] = useState(0);
  const [remembered] = useState(() => readChoice(browserStorage()));
  const [picked, setPicked] = useState<ChatChoice | null>(null);

  useEffect(() => {
    let current = true;
    api.models(packageId).then(
      (data) => current && setModels({ status: 'ready', data }),
      // A failed refresh keeps the models already shown; only a first failure is said.
      (error: unknown) => current && setModels((now) => (now.status === 'ready' ? now : { status: 'error', message: chatFailureWords(error) })),
    );
    return () => {
      current = false;
    };
  }, [api, modelsAsk, packageId]);

  const modelList = models.status === 'ready' ? models.data.models : [];
  const choice = models.status === 'ready' ? resolveChoice(modelList, picked ?? remembered) : null;

  function changeChoice(next: ChatChoice) {
    setPicked(next);
    saveChoice(browserStorage(), next);
  }

  // ── This set's chats ──
  const [list, setList] = useState<ChatListState>({ status: 'loading' });
  const [listAsk, setListAsk] = useState(0);
  const [creating, setCreating] = useState(false);

  useEffect(() => {
    let current = true;
    api.list(packageId).then(
      ({ chats }) => current && setList({ status: 'ready', chats }),
      (error: unknown) => current && setList((now) => (now.status === 'ready' ? now : { status: 'error', message: chatFailureWords(error) })),
    );
    return () => {
      current = false;
    };
  }, [api, packageId, listAsk]);

  // `#/chat/<set>`: open the newest chat, or a new one, in place of this address.
  const [openError, setOpenError] = useState<string | null>(null);
  const [openAsk, setOpenAsk] = useState(0);
  useEffect(() => {
    if (chatId) return;
    let current = true;
    newestOrNewChat(api, packageId).then(
      (id) => current && onOpenChat(id, { replace: true }),
      (error: unknown) => current && setOpenError(chatFailureWords(error)),
    );
    return () => {
      current = false;
    };
  }, [api, packageId, chatId, onOpenChat, openAsk]);

  // ── The composer, and a message on its way ──
  const [draft, setDraft] = useState('');
  const [running, setRunning] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [announce, setAnnounce] = useState('');
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // Chats whose first message was sent here: their title fades in once the server names them.
  const [freshTitles, setFreshTitles] = useState<ReadonlySet<string>>(() => new Set());

  // ── Following the newest words: on until the reviewer scrolls up, back on at the bottom ──
  const scroller = useRef<HTMLDivElement>(null);
  const follow = useRef(true);
  const [atBottom, setAtBottom] = useState(true);

  // ── The open chat ──
  const [loaded, setLoaded] = useState<{ chatId: string; chat: Chat } | null>(null);
  const [loadError, setLoadError] = useState<{ chatId: string; message: string; missing: boolean } | null>(null);
  const [loadAsk, setLoadAsk] = useState(0);
  const [thread, setThread] = useState<{ chatId: string; messages: ViewMessage[] } | null>(null);
  const runRef = useRef<{ chatId: string; controller: AbortController } | null>(null);
  const stopTimer = useRef<number | null>(null);
  const counter = useRef(0);

  useEffect(() => {
    if (!chatId) return;
    // A message running in this chat is its own truth until it ends.
    if (runRef.current?.chatId === chatId) return;
    let current = true;
    api.get(chatId).then(
      (chat) => {
        if (!current) return;
        follow.current = true;
        setLoaded({ chatId, chat });
        setThread({ chatId, messages: chat.messages.map(fromSaved) });
        setLoadError(null);
      },
      (error: unknown) => current && setLoadError({ chatId, message: chatFailureWords(error), missing: error instanceof ApiError && error.status === 404 }),
    );
    return () => {
      current = false;
    };
  }, [api, chatId, loadAsk]);

  // Leaving a chat (or the page) while a message runs stops it: the server saves it as stopped.
  useEffect(() => {
    const runs = runRef;
    return () => runs.current?.controller.abort();
  }, [chatId]);

  const chat = chatId && loaded?.chatId === chatId ? loaded.chat : null;
  const failedToLoad = chatId && loadError?.chatId === chatId && !chat ? loadError : null;
  const messages = chatId && thread?.chatId === chatId ? thread.messages : NO_MESSAGES;
  const name = chat?.package_name ?? null;

  useEffect(() => {
    if (name) onNameChange?.(name);
  }, [name, onNameChange]);

  // A message started elsewhere (another tab, or before a reload) is still being answered: read the
  // chat again every few seconds until its answer is saved.
  const answeredElsewhere = Boolean(chat?.running) && !running;
  useEffect(() => {
    if (!answeredElsewhere) return;
    const timer = window.setTimeout(() => setLoadAsk((n) => n + 1), runningPollMs);
    return () => window.clearTimeout(timer);
  }, [answeredElsewhere, chat, runningPollMs]);

  useLayoutEffect(() => {
    const box = scroller.current;
    if (box && follow.current) box.scrollTop = box.scrollHeight;
  }, [messages]);

  // An empty chat holds the composer in the middle, under a greeting; the first message sends it
  // gliding down to the bottom (it is measured before and after, then slid between the two).
  const activeSummary = list.status === 'ready' ? list.chats.find((candidate) => candidate.id === chatId) : undefined;
  const centred = chat
    ? messages.length === 0 && !answeredElsewhere
    : Boolean(chatId) && !failedToLoad && activeSummary?.turns === 0;
  const dock = useRef<HTMLDivElement>(null);
  const glideFrom = useRef<number | null>(null);
  useLayoutEffect(() => {
    const from = glideFrom.current;
    glideFrom.current = null;
    const box = dock.current;
    if (centred || from === null || !box || reducedMotion()) return;
    const by = from - box.getBoundingClientRect().top;
    if (Math.abs(by) < 1) return;
    box.style.transition = 'none';
    box.style.transform = `translateY(${by}px)`;
    void box.offsetHeight; // Lay out the starting place before sliding from it.
    box.style.transition = 'transform 360ms cubic-bezier(0.2, 0.8, 0.2, 1)';
    box.style.transform = '';
  }, [centred]);

  function onScroll() {
    const box = scroller.current;
    if (!box) return;
    const near = box.scrollHeight - box.scrollTop - box.clientHeight < FOLLOW_GAP;
    follow.current = near;
    setAtBottom(near);
  }

  function jumpToLatest() {
    const box = scroller.current;
    if (!box) return;
    follow.current = true;
    setAtBottom(true);
    box.scrollTo({ top: box.scrollHeight, behavior: reducedMotion() ? 'auto' : 'smooth' });
  }

  // ── Sending and stopping ──

  const spent = models.status === 'ready' ? Number(models.data.package_spent_usd) : NaN;
  const packageCap = models.status === 'ready' ? Number(models.data.package_cap_usd) : NaN;
  const overBudget = Number.isFinite(spent) && Number.isFinite(packageCap) && packageCap > 0 && spent >= packageCap;
  // Why sending is off, in plain words; `shown` reasons get a notice above the composer.
  const unavailable =
    models.status === 'error' ? `The models could not be loaded: ${models.message}`
    : models.status === 'ready' && !models.data.available ? (models.data.reason?.trim() || 'The assistant is not available on this server.')
    : models.status === 'ready' && !choice ? 'No model is offered on this server.'
    : overBudget ? `This drawing set has used its ${dollars(models.status === 'ready' ? models.data.package_cap_usd : null) ?? ''} assistant budget, so no more messages can be sent here.`
    : null;
  const blocked = unavailable ?? (models.status === 'loading' ? 'Loading the models…' : !chat ? 'Opening the chat…' : null);

  async function send(text: string) {
    const question = text.trim();
    if (!question || runRef.current || blocked || !chatId || !choice) return;
    counter.current += 1;
    const userKey = `local-question-${counter.current}`;
    const answerKey = `local-answer-${counter.current}`;
    const controller = new AbortController();
    const run = { chatId, controller };
    runRef.current = run;
    if (centred) glideFrom.current = dock.current?.getBoundingClientRect().top ?? null;
    if (messages.length === 0) setFreshTitles((now) => new Set(now).add(chatId));
    setRunning(true);
    setStopping(false);
    setNotice(null);
    setDraft('');
    setAnnounce('Working on your message');
    follow.current = true;
    setAtBottom(true);
    setThread((now) => ({
      chatId,
      messages: [...(now?.chatId === chatId ? now.messages : []), localQuestion(userKey, question), localAnswer(answerKey, choice)],
    }));
    const patch = (change: (message: ViewMessage) => ViewMessage) =>
      setThread((now) => (now && now.chatId === chatId ? { ...now, messages: now.messages.map((message) => (message.key === answerKey ? change(message) : message)) } : now));

    let started = false;
    try {
      await api.send(
        chatId,
        { text: question, model: choice.model, effort: choice.effort, thinking: choice.thinking },
        (event) => {
          started = true;
          patch((message) => applyChatEvent(message, event));
        },
        controller.signal,
      );
      patch((message) => (message.status === 'running' ? settle(message) : message));
      setAnnounce('Answer ready');
      // The first message names the chat; the set's spending moved on.
      setListAsk((n) => n + 1);
      setModelsAsk((n) => n + 1);
    } catch (error) {
      if (controller.signal.aborted || isAbort(error)) {
        patch((message) => settle(message, { stopped: message.stopped ?? 'cancelled' }));
        setAnnounce('Stopped');
        setListAsk((n) => n + 1);
        setModelsAsk((n) => n + 1);
      } else if (!started) {
        // Refused before anything began (busy, too long, offline): the question goes back to the box.
        setThread((now) => (now && now.chatId === chatId ? { ...now, messages: now.messages.filter((message) => message.key !== userKey && message.key !== answerKey) } : now));
        setDraft((now) => (now.trim() ? now : question));
        const words = chatFailureWords(error);
        setNotice(words);
        setAnnounce(words);
      } else {
        const words = chatFailureWords(error);
        patch((message) => settle(message, { status: 'failed', failure: words }));
        setAnnounce(words);
        setListAsk((n) => n + 1);
      }
    } finally {
      if (stopTimer.current !== null) {
        window.clearTimeout(stopTimer.current);
        stopTimer.current = null;
      }
      if (runRef.current === run) runRef.current = null;
      setRunning(false);
      setStopping(false);
    }
  }

  /** Stop: ask the server to stop (it saves the message as stopped), then stop reading shortly after. */
  function stop() {
    const run = runRef.current;
    if (stopping) return;
    if (!run) {
      // An answer running elsewhere: ask the server to stop it, then read what it saved.
      if (!chatId || !answeredElsewhere) return;
      setStopping(true);
      api.cancel(chatId).catch(() => {}).finally(() => {
        setStopping(false);
        setLoadAsk((n) => n + 1);
      });
      return;
    }
    setStopping(true);
    setAnnounce('Stopping');
    api.cancel(run.chatId).then(
      () => {
        stopTimer.current = window.setTimeout(() => run.controller.abort(), stopGraceMs);
      },
      () => run.controller.abort(),
    );
  }

  async function newChat() {
    if (creating) return;
    // Already on an empty chat: it is the new chat.
    if (chat && messages.length === 0) {
      textareaRef.current?.focus();
      return;
    }
    const empty = list.status === 'ready' ? list.chats.find((candidate) => candidate.turns === 0 && candidate.id !== chatId) : undefined;
    if (empty) {
      onOpenChat(empty.id);
      return;
    }
    setCreating(true);
    try {
      const made = await api.create(packageId);
      setListAsk((n) => n + 1);
      onOpenChat(made.id);
    } catch (error) {
      setNotice(`A new chat could not be started: ${chatFailureWords(error)}`);
    } finally {
      setCreating(false);
    }
  }

  const reload = useCallback(() => setLoadAsk((n) => n + 1), []);
  // What a message may cost, in the model menu's footer.
  const footnote =
    models.status === 'ready' && models.data.available
      ? [
          money(models.data.message_cap_usd) && `Up to ${money(models.data.message_cap_usd)} per answer`,
          money(models.data.package_spent_usd) && money(models.data.package_cap_usd) && `${money(models.data.package_spent_usd)} of ${money(models.data.package_cap_usd)} used on this set`,
        ].filter(Boolean).join(' · ')
      : '';
  const cap = models.status === 'ready' ? models.data.message_cap_usd : null;
  const showScrollDown = !centred && !atBottom && messages.length > 0;

  const chatName = chat?.title?.trim() || activeSummary?.title?.trim() || null;

  return (
    <TooltipProvider delayDuration={300}>
      <div data-tw data-slot="chat-page" className="flex min-h-0 w-full min-w-0 flex-1 font-sans text-sm text-foreground antialiased">
        <HeaderActions>
          <Button variant="ghost" size="sm" asChild>
            <a href={formatRoute({ page: 'review', packageId })} aria-label="Review list">
              <ListChecks /> <span className="hidden sm:inline">Review list</span>
            </a>
          </Button>
        </HeaderActions>
        {/* The set's name is the top bar's; the open chat's title follows it after a "›". */}
        {chatName && (
          <HeaderTitleExtra>
            <span data-slot="chat-crumb" className="flex min-w-0 items-center gap-1.5 text-sm text-muted-foreground">
              <ChevronRight className="size-3.5 shrink-0" aria-hidden="true" />
              <span className="max-w-[18rem] truncate" title={chatName}>{chatName}</span>
            </span>
          </HeaderTitleExtra>
        )}

        {/* One sidebar, like claude.ai: this set's chats sit under the set's name in the app's sidebar. */}
        <SidebarChats>
          <ChatList packageId={packageId} activeChatId={chatId} state={list} creating={creating} onNewChat={() => void newChat()} freshTitles={freshTitles} />
        </SidebarChats>

        <section aria-label={activeSummary ? chatTitle(activeSummary) : 'Conversation'} className="relative flex min-h-0 min-w-0 flex-1 flex-col">
          {/* Positioned, so screen-reader-only text deep in the thread is placed (and clipped) inside it,
              not against the shell, where it would make the whole page scrollable. */}
          <div
            ref={scroller}
            onScroll={onScroll}
            hidden={centred || undefined}
            data-slot="chat-thread"
            className="relative min-h-0 flex-1 overflow-x-hidden overflow-y-auto overscroll-contain"
          >
            {/* Keyed by the chat, so switching chats (or a chat finishing loading) fades the thread in. */}
            <div
              key={`${chatId ?? 'opening'}:${chat ? 'ready' : 'waiting'}`}
              className="mx-auto flex min-h-full w-full max-w-3xl flex-col gap-8 px-4 pt-8 pb-16 motion-safe:animate-in motion-safe:fade-in-0 motion-safe:duration-150"
            >
              {openError && !chatId && (
                <Problem words={`The chats could not be opened: ${openError}`} action="Try again" onAction={() => { setOpenError(null); setOpenAsk((n) => n + 1); }} />
              )}
              {failedToLoad && (
                failedToLoad.missing ? (
                  <Problem words="This chat was not found. It may have been opened from an old link." action="Open this set's newest chat" href={formatRoute({ page: 'chat', packageId })} />
                ) : (
                  <Problem words={`This chat could not be opened: ${failedToLoad.message}`} action="Try again" onAction={reload} />
                )
              )}
              {!chat && !failedToLoad && !openError && (
                <div aria-label="Loading the chat" className="flex flex-col gap-3 pt-6">
                  <Skeleton className="h-7 w-2/3" />
                  <Skeleton className="h-4 w-1/2" />
                </div>
              )}
              {chat && chatId && messages.map((message) =>
                message.role === 'user' ? (
                  <UserMessage key={message.key} message={message} />
                ) : (
                  <AssistantMessage key={message.key} message={message} chatId={chatId} models={modelList} messageCapUsd={cap} onReload={reload} />
                ),
              )}
              {answeredElsewhere && (
                <div data-slot="chat-running-elsewhere">
                  <LiveSteps entries={[]} stage="Still answering the last message (started before this page opened). It shows here when it is done." />
                </div>
              )}
            </div>
          </div>

          <div
            data-slot="chat-dock"
            data-centred={centred ? 'true' : undefined}
            className={cn(
              'relative mx-auto flex w-full max-w-3xl flex-col px-3 sm:px-4',
              centred ? 'flex-1 justify-center gap-0 pb-[14vh]' : 'shrink-0 pt-1 pb-[calc(8px+env(safe-area-inset-bottom,0px))]',
            )}
          >
            {centred && <Greeting name={chat?.package_name ?? null} />}
            {!centred && (
              <button
                type="button"
                onClick={jumpToLatest}
                aria-label="Scroll to bottom"
                title="Scroll to bottom"
                aria-hidden={!showScrollDown || undefined}
                tabIndex={showScrollDown ? 0 : -1}
                data-visible={showScrollDown ? 'true' : 'false'}
                className={cn(
                  'absolute -top-12 left-1/2 z-10 grid size-9 -translate-x-1/2 place-items-center rounded-full border bg-background text-foreground/80 shadow-md transition-[opacity,transform] duration-150 hover:bg-accent hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none motion-reduce:transition-none',
                  showScrollDown ? 'opacity-100' : 'pointer-events-none translate-y-1 opacity-0',
                )}
              >
                <ArrowDown className="size-4" aria-hidden="true" />
              </button>
            )}
            {(unavailable || notice) && (
              <p data-slot="chat-notice" role="alert" className="mb-2 flex items-start gap-2 rounded-xl border bg-muted/40 px-3 py-2 text-[13px]">
                <CircleAlert className="mt-0.5 size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
                <span className="min-w-0 flex-1">{notice ?? unavailable}</span>
                {models.status === 'error' && !notice && (
                  <button type="button" onClick={() => setModelsAsk((n) => n + 1)} className="inline-flex shrink-0 items-center gap-1 text-[13px] font-medium underline-offset-2 hover:underline">
                    <RotateCw className="size-3.5" aria-hidden="true" /> Try again
                  </button>
                )}
              </p>
            )}
            <div ref={dock} data-slot="chat-composer-glide">
              <ChatComposer
                value={draft}
                onChange={setDraft}
                onSend={() => void send(draft)}
                onStop={stop}
                running={running || answeredElsewhere}
                stopping={stopping}
                blocked={blocked}
                models={modelList}
                choice={choice}
                onChoice={changeChoice}
                placeholder={centred ? 'How can I help you today?' : 'Reply to Claude…'}
                menuFootnote={footnote}
                textareaRef={textareaRef}
              />
            </div>
            {centred && <Starters disabled={Boolean(blocked)} onAsk={(question) => void send(question)} />}
            <p data-slot="chat-disclaimer" className={cn('px-2 text-center text-xs text-muted-foreground', centred ? 'mt-5' : 'mt-2')}>
              {DISCLAIMER}
            </p>
          </div>
        </section>

        <p className="sr-only" role="status" aria-live="polite">{announce}</p>
      </div>
    </TooltipProvider>
  );
}

function reducedMotion(): boolean {
  return typeof window === 'undefined' || typeof window.matchMedia !== 'function' || window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

function Problem({ words, action, onAction, href }: { words: string; action: string; onAction?: () => void; href?: string }) {
  const actionClass = 'inline-flex min-h-8 items-center gap-1.5 rounded-lg border bg-background px-3 text-sm font-medium hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none';
  return (
    <div role="alert" className="mt-6 flex flex-col items-start gap-3 rounded-xl border p-4">
      <p className="flex items-start gap-2">
        <CircleAlert className="mt-0.5 size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
        <span>{words}</span>
      </p>
      {href ? <a href={href} className={actionClass}>{action}</a> : <button type="button" onClick={onAction} className={actionClass}>{action}</button>}
    </div>
  );
}

/** An empty chat's greeting above the composer: a small spark and the question. */
function Greeting({ name }: { name: string | null }) {
  return (
    <div data-slot="chat-empty" className="mb-6 flex items-center justify-center gap-3 px-2 motion-safe:animate-in motion-safe:fade-in-0 motion-safe:duration-300">
      {name ? (
        <h2 className="flex items-start gap-2.5 text-center text-[1.6rem] leading-tight font-normal tracking-tight text-balance sm:text-[1.9rem]">
          <Sparkle className="mt-1.5 size-6 shrink-0 fill-current text-foreground/80 sm:size-7" aria-hidden="true" />
          <span>What do you want to know about {name}?</span>
        </h2>
      ) : (
        <Skeleton aria-label="Loading the chat" className="h-9 w-2/3" />
      )}
    </div>
  );
}

/** The three ways to start, as rounded chips under the composer. */
function Starters({ disabled, onAsk }: { disabled: boolean; onAsk: (question: string) => void }) {
  return (
    <ul aria-label="Suggested questions" className="mt-4 flex flex-wrap justify-center gap-2 motion-safe:animate-in motion-safe:fade-in-0 motion-safe:duration-300">
      {CHAT_STARTERS.map((question) => (
        <li key={question} className="min-w-0">
          <button
            type="button"
            disabled={disabled}
            onClick={() => onAsk(question)}
            className="min-h-9 max-w-full rounded-xl border bg-background px-3 py-1.5 text-left text-[13px] text-foreground/85 transition-colors duration-150 hover:bg-accent hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none disabled:opacity-50 disabled:hover:bg-background"
          >
            {question}
          </button>
        </li>
      ))}
    </ul>
  );
}
