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
 * `#/chat/<set>` is a new chat, like claude.ai's: the empty composer, and no chat is made until the
 * first message is sent (then the chat is made, the address becomes `#/chat/<set>/<chat>` in place,
 * and the message is sent). A message streams in event by event; when it ends, the stored message
 * replaces what was built live, so a reload shows exactly the same thing.
 *
 * Like claude.ai, only Stop stops a message (#1479). Leaving the chat, closing the tab or reloading
 * only stops reading it: the server goes on answering and saves the answer. Opening a chat whose
 * last message is still being answered (a reload, another tab) follows it live: the events so far,
 * then each new one, shown exactly like an answer sent from here, with Stop.
 *
 * Read-only toward the review: an answer here is an AI answer ("check before acting") and never
 * becomes a recorded result.
 */

import { Fragment, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { ArrowDown, ChevronRight, CircleAlert, ListChecks, RotateCw } from 'lucide-react';

import { formatRoute } from '@/app/route';
import { HeaderActions, HeaderTitleExtra, SidebarChats } from '@/components/shell/ShellHeader';
import { Button } from '@/components/ui/button';
import { Skeleton } from '@/components/ui/skeleton';
import { TooltipProvider } from '@/components/ui/tooltip';
import { ChatComposer } from '@/components/chat/chat-composer';
import { GVSpark } from '@/components/chat/chat-motion';
import { FullCheckButton, ProductChips } from '@/components/chat/chat-full-check';
import { ChatList, type ChatListState } from '@/components/chat/chat-list';
import { PlusMenu } from '@/components/chat/chat-plus-menu';
import { AssistantMessage, UserMessage } from '@/components/chat/chat-message';
import { UnderstoodLine } from '@/components/chat/chat-understood';
import { ApiError } from '@/api/client';
import { chatFailureWords, chatPaths, CONTINUE_TEXT, isAbort, LIVE_CHAT_API, type Chat, type ChatApi, type ChatEvent, type ChatMessage, type ChatMode, type ChatModels, type ChatPlanAnswer, type ChatProduct, type ChatTaskEdit } from '@/lib/chat-api';
import { confirmWords, effectiveProducts, FULL_CHECK_TEXT, isFullCheck, readProducts, saveProducts, toggleProduct } from '@/lib/chat-full-check';
import { knownAreas, lastFindings, withPrompt } from '@/lib/chat-plus-actions';
import { applyChatEvent, applyCorrection, CHAT_STARTERS, chatTitle, dollars, followedAnswer, fromSaved, localAnswer, localQuestion, previousQuestion, settle, type ViewMessage } from '@/lib/chat-messages';
import { browserStorage, EFFORT_WORDS, readChoice, resolveChoice, saveChoice, shortModelLabel, type ChatChoice } from '@/lib/chat-settings';
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

/** The message this page is reading (sent from here, or followed): `left` once the reviewer moved away. */
interface Reading {
  chatId: string;
  controller: AbortController;
  left: boolean;
}

export interface ChatPageProps {
  packageId: string;
  /** The open chat; null on `#/chat/<set>`, a new chat not made yet. */
  chatId: string | null;
  /** Move to a chat, or (null) to a new one. `replace` swaps the address without a new Back step. */
  onOpenChat: (chatId: string | null, options?: { replace?: boolean }) => void;
  /** The drawing set's name, once known, for the top bar and the tab title. */
  onNameChange?: (name: string) => void;
  api?: ChatApi;
  /** How long Stop waits for the server's stored (stopped) message before it stops reading. Tests pass 0. */
  stopGraceMs?: number;
}

export function ChatPage({ packageId, chatId, onOpenChat, onNameChange, api = LIVE_CHAT_API, stopGraceMs = 3000 }: ChatPageProps) {
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
  // The ids in the newest list read, so a chat opened that it lacks (started elsewhere) is listed.
  const listed = useRef<ReadonlySet<string> | null>(null);
  // The set's name from the list, for a new chat's greeting (no chat is read for it).
  const [listName, setListName] = useState<string | null>(null);
  const [listProduct, setListProduct] = useState<ChatProduct | null>(null);

  useEffect(() => {
    let current = true;
    api.list(packageId).then(
      ({ chats, package_name: setName, product_type: productType }) => {
        listed.current = new Set(chats.map((listedChat) => listedChat.id));
        if (!current) return;
        setList({ status: 'ready', chats });
        if (setName) setListName(setName);
        if (productType) setListProduct(productType);
      },
      (error: unknown) => current && setList((now) => (now.status === 'ready' ? now : { status: 'error', message: chatFailureWords(error) })),
    );
    return () => {
      current = false;
    };
  }, [api, packageId, listAsk]);

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
  const runRef = useRef<Reading | null>(null);
  const stopTimer = useRef<number | null>(null);
  const counter = useRef(0);
  // An answer still being written when its chat was opened: followed live (#1479).
  const [toFollow, setToFollow] = useState<{ chatId: string; messageId: string } | null>(null);

  useEffect(() => {
    if (!chatId) return;
    // A message running in this chat (and still read here) is its own truth until it ends.
    if (runRef.current?.chatId === chatId && !runRef.current.left) return;
    let current = true;
    api.get(chatId).then(
      (chat) => {
        if (!current) return;
        follow.current = true;
        // A chat started elsewhere (another tab, a script) is listed as soon as it is opened.
        if (listed.current && !listed.current.has(chatId)) setListAsk((n) => n + 1);
        setLoaded({ chatId, chat });
        setThread({ chatId, messages: chat.messages.map(fromSaved) });
        setLoadError(null);
        if (chat.running && chat.running_message_id) setToFollow({ chatId, messageId: chat.running_message_id });
      },
      (error: unknown) => current && setLoadError({ chatId, message: chatFailureWords(error), missing: error instanceof ApiError && error.status === 404 }),
    );
    return () => {
      current = false;
    };
  }, [api, chatId, loadAsk]);

  // Leaving a chat (or the page) while a message runs only stops reading it: the server goes on
  // answering and saves the answer; coming back follows it again.
  useEffect(() => {
    const run = runRef.current;
    if (run && run.chatId !== chatId) {
      run.left = true;
      run.controller.abort();
    }
  }, [chatId]);
  useEffect(() => {
    const runs = runRef;
    return () => {
      const run = runs.current;
      if (!run) return;
      run.left = true;
      run.controller.abort();
    };
  }, []);

  const chat = chatId && loaded?.chatId === chatId ? loaded.chat : null;
  const failedToLoad = chatId && loadError?.chatId === chatId && !chat ? loadError : null;
  const messages = chatId && thread?.chatId === chatId ? thread.messages : NO_MESSAGES;
  const name = chat?.package_name ?? listName;

  // ── Products (chat v4): Countertop / Cabinet, remembered per drawing set ──
  const [pickedProducts, setPickedProducts] = useState<{ packageId: string; products: ChatProduct[] } | null>(null);
  const [rememberedProducts] = useState(() => ({ packageId, products: readProducts(browserStorage(), packageId) }));
  const products = effectiveProducts(
    pickedProducts?.packageId === packageId ? pickedProducts.products : null,
    rememberedProducts.packageId === packageId ? rememberedProducts.products : readProducts(browserStorage(), packageId),
    chat?.product_type ?? listProduct,
  );
  function setProducts(next: ChatProduct[]) {
    setPickedProducts({ packageId, products: next });
    saveProducts(browserStorage(), packageId, next);
  }
  const toggle = (product: ChatProduct) => setProducts(toggleProduct(products, product));

  useEffect(() => {
    if (name) onNameChange?.(name);
  }, [name, onNameChange]);

  /** Read one message's events into the answer `key` of `target`'s thread, until it ends. */
  const read = useCallback(
    async (
      run: Reading,
      key: string,
      stream: (onEvent: (event: ChatEvent) => void, signal: AbortSignal) => Promise<ChatMessage>,
      onStarted?: () => void,
    ): Promise<'done' | 'refused'> => {
      const target = run.chatId;
      const patch = (change: (message: ViewMessage) => ViewMessage) =>
        setThread((now) => (now && now.chatId === target ? { ...now, messages: now.messages.map((message) => (message.key === key ? change(message) : message)) } : now));
      let started = false;
      try {
        await stream((event) => {
          if (!started) onStarted?.();
          started = true;
          patch((message) => applyChatEvent(message, event));
          if (event.event === 'finding_update' && event.data.update?.ref) {
            // #1548: the earlier answer it corrects shows the newest version at once.
            const { update } = event.data;
            setThread((now) => (now && now.chatId === target ? { ...now, messages: applyCorrection(now.messages, update) } : now));
          }
        }, run.controller.signal);
        patch((message) => (message.status === 'running' ? settle(message) : message));
        setAnnounce('Answer ready');
        // The first message names the chat; the set's spending moved on.
        setListAsk((n) => n + 1);
        setModelsAsk((n) => n + 1);
        return 'done';
      } catch (error) {
        if (run.left) {
          // Moved away: the server goes on with the message, and shows it again on coming back.
          return 'done';
        }
        if (run.controller.signal.aborted || isAbort(error)) {
          patch((message) => settle(message, { stopped: message.stopped ?? 'cancelled' }));
          setAnnounce('Stopped');
          setListAsk((n) => n + 1);
          setModelsAsk((n) => n + 1);
          return 'done';
        }
        if (!started) return 'refused';
        const words = chatFailureWords(error);
        patch((message) => settle(message, { status: 'failed', failure: words }));
        setAnnounce(words);
        setListAsk((n) => n + 1);
        return 'done';
      } finally {
        // Only the message still read here frees the composer (a chat left and opened again may
        // already be following its message anew).
        if (runRef.current === run) {
          runRef.current = null;
          if (stopTimer.current !== null) {
            window.clearTimeout(stopTimer.current);
            stopTimer.current = null;
          }
          setRunning(false);
          setStopping(false);
        }
      }
    },
    [],
  );

  // Following an answer that was being written when its chat was opened (a reload, another tab):
  // its events so far, then live, exactly like an answer sent from here.
  useEffect(() => {
    if (!toFollow || (runRef.current && !runRef.current.left)) return;
    const { chatId: target, messageId } = toFollow;
    const run: Reading = { chatId: target, controller: new AbortController(), left: false };
    runRef.current = run;
    const key = `follow-${messageId}`;
    setRunning(true);
    setStopping(false);
    setAnnounce('Answer in progress');
    setThread((now) =>
      now && now.chatId === target ? { ...now, messages: [...now.messages.filter((message) => message.key !== key), followedAnswer(messageId)] } : now);
    void read(run, key, (onEvent, signal) => api.follow(target, messageId, onEvent, signal))
      .then((ended) => {
        if (ended === 'refused') {
          // It could not be followed (it ended unsaved, or the connection failed): said, with Reload.
          const words = 'This answer could not be shown live. Reload the chat to see what was saved.';
          setThread((now) => (now && now.chatId === target ? { ...now, messages: now.messages.map((message) => (message.key === key ? settle(message, { status: 'failed', failure: words }) : message)) } : now));
          setAnnounce(words);
        }
      })
      .finally(() => setToFollow((now) => (now === toFollow ? null : now)));
  }, [api, read, toFollow]);

  // The list is read again whenever the window comes back into focus (a chat started in another tab).
  useEffect(() => {
    const again = () => setListAsk((n) => n + 1);
    window.addEventListener('focus', again);
    return () => window.removeEventListener('focus', again);
  }, []);

  useLayoutEffect(() => {
    const box = scroller.current;
    if (box && follow.current) box.scrollTop = box.scrollHeight;
  }, [messages]);

  // An empty chat holds the composer in the middle, under a greeting; the first message sends it
  // gliding down to the bottom (it is measured before and after, then slid between the two).
  const activeSummary = list.status === 'ready' ? list.chats.find((candidate) => candidate.id === chatId) : undefined;
  // A new chat (`#/chat/<set>`, nothing made yet) is centred too, until its first message.
  const centred = !chatId || (chat !== null && messages.length === 0);
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
  const blocked = unavailable ?? (models.status === 'loading' ? 'Loading the models…' : chatId && !chat ? 'Opening the chat…' : null);

  /** `continueFrom` (#1514): the interrupted answer Continue goes on from, and how it was asked. */
  async function send(text: string, mode: ChatMode = 'chat', continueFrom?: { id: string; mode: ChatMode }, task?: ChatTaskEdit, planAnswers?: ChatPlanAnswer[]) {
    const question = text.trim();
    if (!question || runRef.current || blocked || !choice) return;
    counter.current += 1;
    const userKey = `local-question-${counter.current}`;
    const answerKey = `local-answer-${counter.current}`;
    // Held at once, so a second press while the chat is being made does nothing.
    const run: Reading = { chatId: chatId ?? '', controller: new AbortController(), left: false };
    runRef.current = run;
    if (centred) glideFrom.current = dock.current?.getBoundingClientRect().top ?? null;
    setRunning(true);
    setStopping(false);
    setNotice(null);
    setDraft('');
    setAnnounce('Working on your message');

    let target = chatId;
    if (!target) {
      // A new chat is made on its first message, like claude.ai (never an empty one before).
      try {
        const made = await api.create(packageId);
        target = made.id;
        run.chatId = target;
        setLoaded({ chatId: target, chat: { id: target, package_id: packageId, package_name: name ?? '', title: null, created_at: made.created_at, messages: [] } });
        onOpenChat(target, { replace: true });
      } catch (error) {
        if (runRef.current === run) runRef.current = null;
        glideFrom.current = null;
        setRunning(false);
        setDraft((now) => (now.trim() ? now : question));
        const words = `The chat could not be started: ${chatFailureWords(error)}`;
        setNotice(words);
        setAnnounce(words);
        return;
      }
    }
    const sendTo = target;
    if (messages.length === 0) setFreshTitles((now) => new Set(now).add(sendTo));
    follow.current = true;
    setAtBottom(true);
    setThread((now) => ({
      chatId: sendTo,
      messages: [
        ...(now?.chatId === sendTo ? now.messages : []),
        localQuestion(userKey, question, new Date(), { mode, products, effort: choice.effort }),
        localAnswer(answerKey, choice, new Date(), { mode: continueFrom?.mode ?? mode, products }),
      ],
    }));

    let refusal: unknown = null;
    const ended = await read(
      run,
      answerKey,
      (onEvent, signal) =>
        api.send(sendTo, {
          text: question, model: choice.model, effort: choice.effort, thinking: choice.thinking,
          ...(mode === 'full_check' ? { mode } : {}),
          ...(products ? { products } : {}),
          ...(continueFrom ? { continue_from: continueFrom.id } : {}),
          ...(task ? { task } : {}),
          ...(mode === 'full_check' && planAnswers?.length ? { plan_answers: planAnswers } : {}),
        }, onEvent, signal).catch((error: unknown) => {
          refusal = error;
          throw error;
        }),
      // The question is saved before `message_start`: list the chat now, titled by it.
      () => setListAsk((n) => n + 1),
    );
    if (ended === 'refused') {
      // Refused before anything began (busy, too long, offline): the question goes back to the box.
      setThread((now) => (now && now.chatId === sendTo ? { ...now, messages: now.messages.filter((message) => message.key !== userKey && message.key !== answerKey) } : now));
      if (mode === 'chat' && !continueFrom) setDraft((now) => (now.trim() ? now : question));
      const words = chatFailureWords(refusal);
      setNotice(words);
      setAnnounce(words);
    }
  }

  /** Stop: ask the server to stop (it saves the message as stopped), then stop reading shortly after. */
  function stop() {
    const run = runRef.current;
    if (stopping || !run || !run.chatId) return;
    setStopping(true);
    setAnnounce('Stopping');
    api.cancel(run.chatId).then(
      () => {
        stopTimer.current = window.setTimeout(() => run.controller.abort(), stopGraceMs);
      },
      () => run.controller.abort(),
    );
  }

  /** New chat: the empty composer at `#/chat/<set>`; nothing is made until its first message. */
  function newChat() {
    if (!chatId) {
      textareaRef.current?.focus();
      return;
    }
    onOpenChat(null);
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

  // ── Full check (chat v4): one button, confirmed with what it will use ──
  const pickedModel = choice ? modelList.find((model) => model.id === choice.model) ?? null : null;
  const fullCheckSummary = confirmWords(products, pickedModel ? shortModelLabel(pickedModel) : null, choice ? EFFORT_WORDS[choice.effort] : null, money(cap));
  const fullCheckBlocked = blocked ?? (running ? 'A message is still running in this chat.' : null);
  const runFullCheck = (answers?: ChatPlanAnswer[]) => void send(FULL_CHECK_TEXT, 'full_check', undefined, undefined, answers);
  // #1569: the plan check before a paid full check (a fake API may leave it out: no questions).
  const planApi = api.plan;
  const loadPlan = useMemo(() => (planApi ? () => planApi(packageId) : undefined), [planApi, packageId]);

  return (
    <TooltipProvider delayDuration={300}>
      <div data-tw data-slot="chat-page" className="flex min-h-0 w-full min-w-0 flex-1 font-sans text-sm text-foreground antialiased">
        <HeaderActions>
          {!centred && <FullCheckButton variant="bar" summary={fullCheckSummary} onRun={runFullCheck} blocked={fullCheckBlocked} loadPlan={loadPlan} />}
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
          <ChatList packageId={packageId} activeChatId={chatId} state={list} creating={false} onNewChat={newChat} freshTitles={freshTitles} />
        </SidebarChats>

        <section aria-label={activeSummary ? chatTitle(activeSummary) : 'Conversation'} data-slot="chat-page" className="chat-brand relative flex min-h-0 w-full min-w-0 flex-1 flex-col overflow-x-clip">
          {/* Positioned, so screen-reader-only text deep in the thread is placed (and clipped) inside it,
              not against the shell, where it would make the whole page scrollable. */}
          <div
            ref={scroller}
            onScroll={onScroll}
            hidden={centred || undefined}
            data-slot="chat-thread"
            className="relative min-h-0 flex-1 overflow-x-hidden overflow-y-auto overscroll-contain [container-type:inline-size]"
          >
            {/* Keyed by the chat, so switching chats (or a chat finishing loading) fades the thread in. */}
            <div
              key={`${chatId ?? 'new'}:${chat ? 'ready' : 'waiting'}`}
              className="mx-auto flex min-h-full w-full max-w-[63rem] min-w-0 flex-col gap-8 px-4 pt-8 pb-16 sm:px-6 motion-safe:animate-in motion-safe:fade-in-0 motion-safe:duration-150"
            >
              {failedToLoad && (
                failedToLoad.missing ? (
                  <Problem words="This chat was not found. It may have been opened from an old link." action="Start a new chat" href={formatRoute({ page: 'chat', packageId })} />
                ) : (
                  <Problem words={`This chat could not be opened: ${failedToLoad.message}`} action="Try again" onAction={reload} />
                )
              )}
              {chatId && !chat && !failedToLoad && (
                <div aria-label="Loading the chat" className="flex flex-col gap-3 pt-6">
                  <Skeleton className="h-7 w-2/3" />
                  <Skeleton className="h-4 w-1/2" />
                </div>
              )}
              {chat && chatId && messages.map((message, at) => {
                if (message.role === 'user') {
                  // A saved full check's bubble names the effort its answer ran at.
                  const answer = messages[at + 1];
                  // #1551: how the app understood a typed question, with Edit to correct it and resend.
                  const understood = answer?.role === 'assistant' && message.mode !== 'full_check' ? answer.understood : null;
                  return (
                    <Fragment key={message.key}>
                      <UserMessage message={message} effort={message.effort ?? (answer?.role === 'assistant' ? answer.effort : null)} />
                      {understood && (
                        <UnderstoodLine
                          understood={understood}
                          disabled={running || Boolean(blocked)}
                          onResend={(task) => void send(message.text, 'chat', undefined, task)}
                        />
                      )}
                    </Fragment>
                  );
                }
                const asked = messages[at - 1];
                // Retry asks the answer's question again, as a new message (claude.ai's Retry).
                const question = previousQuestion(messages, message.key);
                const canRetry = question !== null && !running && !blocked;
                // Continue (#1514): only the newest answer, when a server restart interrupted it.
                const resumeId = message.stopped === 'interrupted' && message.id && at === messages.length - 1 && !running && !blocked ? message.id : null;
                return (
                  <AssistantMessage
                    key={message.key}
                    message={message}
                    chatId={chatId}
                    models={modelList}
                    messageCapUsd={cap}
                    onReload={reload}
                    onRetry={canRetry ? () => void send(question) : undefined}
                    onContinue={resumeId ? () => void send(CONTINUE_TEXT, 'chat', { id: resumeId, mode: message.resume?.mode ?? 'chat' }) : undefined}
                    latest={at === messages.length - 1}
                    makeMarkupPdf={(messageId) => api.markup(chatId, messageId)}
                    decide={api.decide ? (messageId, findingId, decision) => api.decide!(chatId, messageId, findingId, decision) : undefined}
                    remakeCloud={api.remakeCloud ? (messageId) => api.remakeCloud!(chatId, messageId) : undefined}
                    fullPageHref={formatRoute({ page: 'review', packageId })}
                    fullCheck={isFullCheck(message, asked?.role === 'user' ? asked : null)}
                  />
                );
              })}
            </div>
          </div>

          <div
            data-slot="chat-dock"
            data-centred={centred ? 'true' : undefined}
            className={cn(
              'relative mx-auto flex w-full max-w-[63rem] min-w-0 flex-col px-4 sm:px-6',
              centred ? 'flex-1 justify-center gap-0 pb-[14vh]' : 'shrink-0 pt-1 pb-[calc(8px+env(safe-area-inset-bottom,0px))]',
            )}
          >
            {centred && <Greeting name={name} />}
            {centred && (
              <div className="mb-4 flex flex-col items-center gap-1.5">
                <FullCheckButton summary={fullCheckSummary} onRun={runFullCheck} blocked={fullCheckBlocked} loadPlan={loadPlan} />
                <p className="text-center text-xs text-muted-foreground">What’s wrong, what to check and what’s right on every page, with the cloud drawing</p>
              </div>
            )}
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
                running={running}
                stopping={stopping}
                blocked={blocked}
                models={modelList}
                choice={choice}
                onChoice={changeChoice}
                placeholder={centred ? 'How can I help you today?' : 'Reply to Claude…'}
                menuFootnote={footnote}
                textareaRef={textareaRef}
                leading={<ProductChips products={products} onToggle={toggle} disabled={running} />}
                plus={
                  <PlusMenu
                    onPrompt={(text) => setDraft((now) => withPrompt(now, text))}
                    textareaRef={textareaRef}
                    fullCheck={{ summary: fullCheckSummary, blocked: fullCheckBlocked, onRun: runFullCheck, loadPlan }}
                    lastFindings={lastFindings(messages)}
                    makeMarkupPdf={chatId ? (messageId) => api.markup(chatId, messageId) : null}
                    fileHref={(file) => file.url || (chatId ? chatPaths.file(chatId, file.id) : '')}
                    products={products}
                    onProducts={setProducts}
                    models={modelList}
                    choice={choice}
                    onChoice={changeChoice}
                    messageCap={money(cap)}
                    areas={knownAreas(messages)}
                    onNotice={setNotice}
                    running={running}
                  />
                }
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
    <div data-slot="chat-empty" className="mb-6 flex min-w-0 justify-center motion-safe:animate-in motion-safe:fade-in-0 motion-safe:duration-300">
      {name ? (
        // #1538: centred in the column, at most two balanced lines; a very long set name is cut
        // with "…" (the whole question is on hover), never run off the edge.
        <h2
          title={`What do you want to know about ${name}?`}
          className="line-clamp-2 max-w-[42rem] min-w-0 text-center text-[1.6rem] leading-tight font-normal tracking-tight text-balance [overflow-wrap:anywhere] sm:text-[1.9rem]"
        >
          <GVSpark state="done" size={28} className="mr-2.5 inline-grid align-[-0.2em]" />
          What do you want to know about {name}?
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
