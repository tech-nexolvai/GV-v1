import { useEffect, useId, useRef, useState } from 'react';
import { ArrowUp, ChevronRight, CircleAlert, CircleHelp, FileX, ListChecks, MessageSquare, Square, SquarePen, TriangleAlert, X } from 'lucide-react';

import { getAssistant, streamAssistant } from '@/api/client';
import type { AssistantAnswer, AssistantInfo } from '@/api/assistantTypes';
import { cn } from '@/lib/utils';
import { ASSISTANT_FULL_SCREEN, useMediaQuery } from '@/hooks/use-media-query';
import { failureWords, focusOf, historyFor, sameContext, starterHint, type AssistantContext, type AssistantRecords, type StarterKind } from '@/lib/assistant';
import { AnswerView, type AssistantNavigation } from '@/components/assistant/assistant-answer';

export type { AssistantNavigation } from '@/components/assistant/assistant-answer';

/** One question and what came back. */
export interface AssistantTurn {
  id: string;
  question: string;
  /** The page or countertop sent with the question, if any. */
  context: AssistantContext | null;
  status: 'working' | 'done' | 'error' | 'stopped';
  /** The server's current stage label while working; each replaces the last. */
  stage: string | null;
  answer: AssistantAnswer | null;
  error: string | null;
  /** True for an answer that just arrived: its words are shown a few at a time. */
  reveal: boolean;
}

export type AssistantInfoState = { status: 'loading' } | { status: 'ready'; info: AssistantInfo } | { status: 'error' };

/** The calls the panel makes. The review page uses the real client; the UI kit passes made-up ones. */
export interface AssistantApi {
  info: (projectId: string, packageId: string) => Promise<AssistantInfo>;
  stream: typeof streamAssistant;
}

const LIVE_API: AssistantApi = { info: getAssistant, stream: streamAssistant };

const STARTER_ICON: Record<StarterKind, typeof CircleHelp> = {
  why: CircleHelp,
  left: ListChecks,
  held: TriangleAlert,
  none: FileX,
  ask: MessageSquare,
};

function isAbort(error: unknown): boolean {
  return error instanceof DOMException ? error.name === 'AbortError' : error instanceof Error && error.name === 'AbortError';
}

let turnCounter = 0;

export interface AssistantPanelProps {
  projectId: string;
  packageId: string;
  /** The drawing set's name, for the header line. */
  setName: string;
  open: boolean;
  records: AssistantRecords;
  /** What the reviewer is looking at now (drawing, queue item, countertop card), or null. */
  context: AssistantContext | null;
  nav: AssistantNavigation;
  onClose: () => void;
  /** `docked`: beside the review on a desktop, the whole screen on a phone. `inline`: fills its box (UI kit). */
  layout?: 'docked' | 'inline';
  api?: AssistantApi;
  /** Start with these turns and this info (UI kit). Without them the panel asks the server. */
  initialTurns?: AssistantTurn[];
  initialInfo?: AssistantInfoState;
  /** The panel's element id (the header button points at it). */
  id?: string;
  /** Move focus into the composer when the panel opens (off where several panels show at once). */
  autoFocus?: boolean;
}

/**
 * The review assistant (#1129): ask about this set in plain words. It answers from this review's
 * records only, points at pages and queue items, and never records a decision.
 */
export function AssistantPanel({
  projectId,
  packageId,
  setName,
  open,
  records,
  context,
  nav,
  onClose,
  layout = 'docked',
  api = LIVE_API,
  initialTurns,
  initialInfo,
  id = 'review-assistant',
  autoFocus = true,
}: AssistantPanelProps) {
  const questionId = useId();
  const [info, setInfo] = useState<AssistantInfoState>(initialInfo ?? { status: 'loading' });
  const [turns, setTurns] = useState<AssistantTurn[]>(initialTurns ?? []);
  const [draft, setDraft] = useState('');
  // The context the reviewer took off; a different page or countertop brings the chip back.
  const [dismissed, setDismissed] = useState<AssistantContext | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const threadRef = useRef<HTMLDivElement>(null);
  const rootRef = useRef<HTMLElement>(null);
  // Docked beside the review when there is room (see the classes below); laid over it otherwise; the
  // whole screen at 900px and below.
  const fullScreen = useMediaQuery(ASSISTANT_FULL_SCREEN) && layout === 'docked';

  const busy = turns.some((turn) => turn.status === 'working');
  const chip = context && !sameContext(context, dismissed) ? context : null;

  useEffect(() => {
    if (initialInfo) return;
    let current = true;
    api.info(projectId, packageId).then(
      (answer) => current && setInfo({ status: 'ready', info: answer }),
      () => current && setInfo({ status: 'error' }),
    );
    return () => {
      current = false;
    };
  }, [api, projectId, packageId, initialInfo]);

  // Into the composer on open, so the reviewer can type at once.
  useEffect(() => {
    if (open && autoFocus) textareaRef.current?.focus();
  }, [open, autoFocus]);

  // A stream still running when the review closes is stopped.
  useEffect(() => () => abortRef.current?.abort(), []);

  // Covering the whole screen, the panel is a modal: everything outside it is inert until it closes.
  useEffect(() => {
    const panel = rootRef.current;
    if (!open || !fullScreen || !panel) return;
    const made: Element[] = [];
    for (let node: Element = panel; node.parentElement && node !== document.body; node = node.parentElement) {
      for (const sibling of node.parentElement.children) {
        if (sibling !== node && !sibling.hasAttribute('inert') && sibling.tagName !== 'SCRIPT') {
          sibling.setAttribute('inert', '');
          made.push(sibling);
        }
      }
    }
    return () => made.forEach((element) => element.removeAttribute('inert'));
  }, [open, fullScreen]);

  // Keep the newest message in view.
  const last = turns[turns.length - 1];
  useEffect(() => {
    const thread = threadRef.current;
    if (thread) thread.scrollTop = thread.scrollHeight;
  }, [turns.length, last?.status, last?.stage]);

  function patch(id: string, change: Partial<AssistantTurn>) {
    setTurns((current) => current.map((turn) => (turn.id === id ? { ...turn, ...change } : turn)));
  }

  async function ask(question: string, sendContext: AssistantContext | null = chip, before: readonly AssistantTurn[] = turns) {
    const text = question.trim();
    if (!text || busy) return;
    turnCounter += 1;
    const turnId = `turn-${turnCounter}`;
    const controller = new AbortController();
    abortRef.current = controller;
    setTurns([...before, { id: turnId, question: text, context: sendContext, status: 'working', stage: null, answer: null, error: null, reveal: true }]);
    try {
      const answer = await api.stream(
        projectId,
        packageId,
        {
          question: text.slice(0, 500),
          history: historyFor(before.filter((turn) => turn.status === 'done')),
          ...(sendContext ? { focus: focusOf(sendContext) } : {}),
        },
        { onStage: (stage) => patch(turnId, { stage: stage.label }) },
        controller.signal,
      );
      patch(turnId, { status: 'done', answer, stage: null });
    } catch (error) {
      patch(turnId, controller.signal.aborted || isAbort(error) ? { status: 'stopped', stage: null } : { status: 'error', error: failureWords(error), stage: null });
    } finally {
      if (abortRef.current === controller) abortRef.current = null;
    }
  }

  function resize() {
    const box = textareaRef.current;
    if (!box) return;
    box.style.height = 'auto';
    box.style.height = `${Math.min(box.scrollHeight, 140)}px`;
  }

  function submit() {
    if (!draft.trim() || busy) return;
    const question = draft;
    setDraft('');
    window.requestAnimationFrame(resize);
    void ask(question);
  }

  function retry(turn: AssistantTurn) {
    void ask(turn.question, turn.context, turns.filter((t) => t.id !== turn.id));
  }

  /** Stop the running answer. A working turn with no request behind it (a UI-kit specimen) just stops. */
  function stop() {
    if (abortRef.current) abortRef.current.abort();
    else setTurns((current) => current.map((turn) => (turn.status === 'working' ? { ...turn, status: 'stopped', stage: null } : turn)));
  }

  function newChat() {
    abortRef.current?.abort();
    abortRef.current = null;
    setTurns([]);
    setDraft('');
    setDismissed(null);
    textareaRef.current?.focus();
  }

  const starters = info.status === 'ready' ? info.info.starters : [];
  const footer =
    info.status === 'ready'
      ? `${info.info.model_label}${info.info.keeps_no_data ? ' · keeps no data' : ''} · it explains, you decide`
      : 'It explains, you decide';
  // Errors are announced by their own alert; "Answer ready" only once the words are all on screen.
  const status =
    last?.status === 'working' ? last.stage ?? 'Sending your question'
    : last?.status === 'done' && !last.reveal ? 'Answer ready'
    : last?.status === 'stopped' ? 'Stopped.'
    : '';

  return (
    <aside
      ref={rootRef}
      id={id}
      data-tw
      data-slot="assistant-panel"
      aria-label="Assistant"
      role={fullScreen ? 'dialog' : undefined}
      aria-modal={fullScreen ? true : undefined}
      hidden={!open}
      onKeyDown={(event) => {
        if (event.key === 'Escape' && !event.defaultPrevented) {
          event.preventDefault();
          onClose();
        }
      }}
      className={cn(
        'flex-col bg-background font-sans text-sm text-foreground antialiased',
        open ? 'flex' : 'hidden',
        layout === 'docked'
          ? cn(
              // Up to 900px: the whole screen.
              'fixed inset-0 z-40 h-dvh',
              // Wider than 900px but not room to dock: laid over the review's right side, like a sheet.
              'min-[901px]:left-auto min-[901px]:w-[min(420px,48vw)] min-[901px]:border-l min-[901px]:shadow-xl',
              // Docked beside the review only where the review keeps at least 760px: the review page's
              // own width (container "review", which excludes the sidebar) is 1180px or more.
              '@min-[1180px]/review:static @min-[1180px]/review:z-auto @min-[1180px]/review:h-full @min-[1180px]/review:w-[420px] @min-[1180px]/review:shrink-0 @min-[1180px]/review:shadow-none',
            )
          : 'h-full w-full min-w-0',
      )}
    >
      <header className="flex items-center gap-2.5 border-b py-3 pr-3 pl-4">
        <div className="min-w-0 flex-1 leading-tight">
          <h2 className="text-[15px] font-semibold">Assistant</h2>
          <p className="truncate text-xs text-muted-foreground">{setName} · answers from this review only</p>
        </div>
        <button
          type="button"
          onClick={newChat}
          title="New chat"
          aria-label="New chat"
          className="grid size-[34px] shrink-0 place-items-center rounded-lg text-muted-foreground hover:bg-accent hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
        >
          <SquarePen className="size-[17px]" aria-hidden="true" />
        </button>
        <button
          type="button"
          onClick={onClose}
          title="Close (Esc)"
          aria-label="Close assistant"
          className="grid size-[34px] shrink-0 place-items-center rounded-lg text-muted-foreground hover:bg-accent hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
        >
          <X className="size-[17px]" aria-hidden="true" />
        </button>
      </header>

      <div ref={threadRef} data-slot="assistant-thread" className="flex min-h-0 flex-1 flex-col gap-6 overflow-x-hidden overflow-y-auto overscroll-contain px-4 pt-5 pb-3">
        {turns.length === 0 && (
          <Starters
            info={info}
            starters={starters}
            records={records}
            busy={busy}
            onAsk={(question) => void ask(question)}
          />
        )}
        {turns.map((turn, index) => {
          const latest = index === turns.length - 1;
          return (
            <div key={turn.id} className="flex min-w-0 flex-col gap-6">
              <div data-slot="assistant-question" className="max-w-[85%] self-end rounded-[16px_16px_4px_16px] bg-muted px-3.5 py-2 break-words whitespace-pre-line">
                {turn.context && (
                  <span className="block text-xs text-muted-foreground">
                    About page <span className="num">{turn.context.page_number}</span>
                  </span>
                )}
                {turn.question}
              </div>
              {turn.status === 'working' && (
                <div data-slot="assistant-working" className="flex items-center gap-2.5 text-[13px] text-muted-foreground">
                  <span aria-hidden="true" className="size-3.5 shrink-0 animate-spin rounded-full border-[1.5px] border-border border-t-foreground motion-reduce:animate-none" />
                  {turn.stage ?? 'Sending your question'}
                </div>
              )}
              {turn.status === 'done' && turn.answer && (
                <>
                  <AnswerView
                    answer={turn.answer}
                    records={records}
                    nav={nav}
                    reveal={turn.reveal}
                    latest={latest}
                    busy={busy}
                    onAsk={(question) => void ask(question)}
                    onRevealed={() => patch(turn.id, { reveal: false })}
                  />
                  {latest && turn.answer.mode === 'disabled' && starters.length > 0 && (
                    <Starters info={info} starters={starters} records={records} busy={busy} onAsk={(question) => void ask(question)} bare />
                  )}
                </>
              )}
              {(turn.status === 'error' || turn.status === 'stopped') && (
                <div data-slot="assistant-error" role={turn.status === 'error' ? 'alert' : undefined} className="flex flex-wrap items-center gap-x-3 gap-y-2 text-sm">
                  <span className="inline-flex min-w-0 items-start gap-2">
                    <CircleAlert className="mt-0.5 size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
                    <span>{turn.status === 'stopped' ? 'Stopped.' : turn.error}</span>
                  </span>
                  {latest && (
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => retry(turn)}
                      className="min-h-8 rounded-lg border bg-background px-3 text-sm font-medium hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none disabled:opacity-50"
                    >
                      Try again
                    </button>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>
      <p className="sr-only" role="status" aria-live="polite">
        {status}
      </p>

      <div className="grid gap-2 px-3 pt-2.5 pb-[calc(10px+env(safe-area-inset-bottom,0px))]">
        <form
          data-slot="assistant-composer"
          className="grid gap-1 rounded-[18px] border bg-background py-1.5 pr-1.5 pl-2 shadow-xs focus-within:border-input"
          onSubmit={(event) => {
            event.preventDefault();
            submit();
          }}
        >
          {chip && (
            <span data-slot="assistant-context" className="ml-1 inline-flex items-center gap-1.5 justify-self-start rounded-lg bg-muted py-0.5 pr-0.5 pl-2.5 text-xs">
              <span>
                Page <span className="num">{chip.page_number}</span>
              </span>
              <button
                type="button"
                onClick={() => {
                  setDismissed(chip);
                  textareaRef.current?.focus();
                }}
                aria-label={`Stop asking about page ${chip.page_number}`}
                title="Ask about the whole set"
                className="grid size-7 place-items-center rounded-md text-muted-foreground hover:bg-accent hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
              >
                <X className="size-3" aria-hidden="true" />
              </button>
            </span>
          )}
          <div className="flex items-end gap-1.5">
            <label htmlFor={questionId} className="sr-only">
              Your question
            </label>
            <textarea
              id={questionId}
              ref={textareaRef}
              rows={1}
              value={draft}
              maxLength={500}
              placeholder="Ask about a page or what is left"
              onChange={(event) => {
                setDraft(event.target.value);
                resize();
              }}
              onKeyDown={(event) => {
                if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
                  event.preventDefault();
                  submit();
                }
              }}
              className="max-h-[140px] min-w-0 flex-1 resize-none border-0 bg-transparent px-1.5 py-[7px] text-[14.5px] leading-[1.45] text-foreground outline-none placeholder:text-muted-foreground"
            />
            {busy ? (
              <button
                type="button"
                onClick={stop}
                aria-label="Stop"
                title="Stop"
                className="grid size-[34px] shrink-0 place-items-center rounded-full bg-primary text-primary-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:outline-none"
              >
                <Square className="size-3 fill-current" aria-hidden="true" />
              </button>
            ) : (
              <button
                type="submit"
                aria-label="Send"
                title="Send"
                disabled={!draft.trim()}
                className="grid size-[34px] shrink-0 place-items-center rounded-full bg-primary text-primary-foreground transition-opacity focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:outline-none disabled:cursor-default disabled:opacity-25"
              >
                <ArrowUp className="size-4" aria-hidden="true" />
              </button>
            )}
          </div>
        </form>
        <p className="text-center text-xs text-muted-foreground">{footer}</p>
      </div>
    </aside>
  );
}

function Starters({
  info,
  starters,
  records,
  busy,
  onAsk,
  bare = false,
}: {
  info: AssistantInfoState;
  starters: readonly string[];
  records: AssistantRecords;
  busy: boolean;
  onAsk: (question: string) => void;
  /** Only the questions (after a "switched off" answer). */
  bare?: boolean;
}) {
  const list = (
    <ul aria-label="Suggested questions" className="grid gap-2">
      {starters.map((question) => {
        const hint = starterHint(question, records);
        const Icon = STARTER_ICON[hint.kind];
        return (
          <li key={question}>
            <button
              type="button"
              disabled={busy}
              onClick={() => onAsk(question)}
              className="flex min-h-12 w-full items-center gap-3 rounded-xl border bg-background px-3.5 py-2.5 text-left hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none disabled:opacity-50"
            >
              <span className="grid size-7 shrink-0 place-items-center rounded-lg bg-muted text-muted-foreground" aria-hidden="true">
                <Icon className="size-[15px]" />
              </span>
              <span className="min-w-0 flex-1">
                {question}
                {hint.reason && <span className="block text-xs text-muted-foreground">{hint.reason}</span>}
              </span>
              <ChevronRight className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
            </button>
          </li>
        );
      })}
    </ul>
  );
  if (bare) return list;
  return (
    <div data-slot="assistant-empty" className="mt-auto grid gap-[18px]">
      <div>
        <h3 className="text-xl leading-tight font-semibold text-balance">Ask about this set</h3>
        <p className="mt-1.5 max-w-[40ch] text-[13.5px] text-muted-foreground">
          It explains and points. Pass or fail comes only from the checks; decisions stay with you.
        </p>
      </div>
      {info.status === 'ready' && !info.info.enabled && (
        <p className="text-[13.5px] text-muted-foreground">The model is switched off on this server. Questions the records can answer still get an answer.</p>
      )}
      {info.status === 'error' && <p className="text-[13.5px] text-muted-foreground">Suggested questions could not be loaded. You can still ask your own.</p>}
      {starters.length > 0 && list}
    </div>
  );
}
