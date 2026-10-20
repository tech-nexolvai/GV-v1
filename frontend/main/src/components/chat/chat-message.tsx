import { useEffect, useId, useRef, useState } from 'react';
import { ArrowRightLeft, ChevronRight, CircleAlert, Download, FileDown, Sheet, FileImage, FileSpreadsheet, FileText, File as FileIcon, Play, RotateCw, ScanSearch } from 'lucide-react';

import { ApiError } from '@/api/client';
import { AccuracyBadge, CiteChips } from '@/components/chat/chat-accuracy';
import { ActivityRow, TaskChecklist } from '@/components/chat/chat-activity';
import { CoverageStrip } from '@/components/chat/chat-coverage';
import { EvidenceViewer } from '@/components/chat/chat-evidence';
import { FindingsSections, type DecisionProps } from '@/components/chat/chat-findings';
import { FullCheckProgress, type CloudReview } from '@/components/chat/chat-full-check';
import { LiveCheck } from '@/components/chat/chat-live';
import { FullCheckResult } from '@/components/chat/chat-result';
import { ConfidenceLine, HowChecked } from '@/components/chat/chat-trust';
import { ChatMarkdown } from '@/components/chat/chat-markdown';
import { Collapse, CopyButton, GVSpark, Shimmer, Spark, Spinner, type SparkState } from '@/components/chat/chat-motion';
import { chatPaths, type ChatEffort, type ChatFile, type ChatFinding, type ChatFindingUpdate, type ChatModel, type FindingDecision, type FindingDecisionChoice, type FindingDecisionExtra, type RemadeFiles } from '@/lib/chat-api';
import { coverageCounts, isNarrowAnswer, isShortAnswer, NO_FILTER, sectionOrder, type FindingFilter } from '@/lib/chat-findings';
import { fullCheckBubble, fullCheckFiles, withoutCodeNote } from '@/lib/chat-full-check';
import { commentNumber, resultGroups } from '@/lib/chat-result';
import { asShown, proofGroup } from '@/lib/chat-proof';
import { EFFORT_WORDS } from '@/lib/chat-settings';
import { startingLive } from '@/lib/chat-live';
import { useReducedMotion } from '@/lib/chat-motion';
import { downloadFile, downloadFindingsCsv, markupFailureWords } from '@/lib/chat-exports';
import { hasTrust, roundCounts } from '@/lib/chat-trust';
import {
  answerItems,
  fileKind,
  fileSize,
  fallbackWords,
  footerWords,
  messageCost,
  openHref,
  shortModelWords,
  shownLive,
  shownBlocks,
  stoppedWords,
  textStreaming,
  thinkingLine,
  thoughtFor,
  withoutStopNote,
  type ViewBlock,
  type ViewMessage,
} from '@/lib/chat-messages';
import { cn } from '@/lib/utils';

/**
 * One message of a chat (#1464), made to look and move like claude.ai (#1470). The question is a
 * rounded bubble on the right. The answer has no bubble: full-width words, with the work behind
 * them folded into rows ("Thought for 7s, ran 2 commands") in the order it happened, its task
 * list, the files it made, and, once finished, Copy and Retry with a small line naming the model,
 * effort and cost. While it works, a spark turns at its foot.
 * Read-only: nothing here changes the review.
 */

export function UserMessage({ message, effort }: { message: ViewMessage; effort?: ChatEffort | null }) {
  if (message.mode === 'full_check') {
    // Chat v4: a full check is the app's own instruction, so the bubble names it, not its text.
    const level = effort ?? message.effort;
    return (
      <div
        data-slot="chat-user-message"
        data-mode="full_check"
        className="chat-rise inline-flex max-w-[min(85%,36rem)] items-center gap-2 self-end rounded-2xl bg-muted px-4 py-2.5 font-medium text-foreground"
      >
        <ScanSearch className="size-4 shrink-0" aria-hidden="true" />
        <span>{fullCheckBubble(message.products, level ? EFFORT_WORDS[level] : null)}</span>
      </div>
    );
  }
  return (
    <div
      data-slot="chat-user-message"
      className="chat-rise max-w-[min(85%,36rem)] self-end rounded-2xl bg-muted px-4 py-2.5 leading-relaxed break-words whitespace-pre-wrap text-foreground"
    >
      {message.text}
    </div>
  );
}

// ── Thinking on its own ─────────────────────────────────────

/**
 * A thinking block shown on its own (not inside an activity row): "Thinking…" shimmering with its
 * newest line while it streams, "Thought for 12s" with a chevron after. Opening it grows its
 * height and shows the thinking, smaller and muted, with a rule on its left.
 */
export function ThinkingBlock({ block }: { block: Extract<ViewBlock, { type: 'thinking' }> }) {
  const [open, setOpen] = useState(false);
  const bodyId = useId();
  const title = block.live ? thinkingLine(block.text) ?? 'Thinking…' : thoughtFor(block.seconds);
  if (!block.text.trim() && !block.live) {
    return <p data-slot="chat-thinking" className="text-sm text-muted-foreground">{title}</p>;
  }
  return (
    <div data-slot="chat-thinking" data-live={block.live ? 'true' : 'false'} className="min-w-0">
      <button
        type="button"
        aria-expanded={open}
        aria-controls={bodyId}
        onClick={() => setOpen(!open)}
        className="flex w-fit max-w-full min-w-0 items-center gap-1.5 rounded-md py-1 text-left text-sm text-muted-foreground transition-colors hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
      >
        <GVSpark state={block.live ? 'thinking' : 'still'} size={16} />
        <Shimmer active={block.live} className="min-w-0 truncate">{title}</Shimmer>
        <ChevronRight aria-hidden="true" className={cn('size-3.5 shrink-0 transition-transform duration-200 ease-out motion-reduce:transition-none', open && 'rotate-90')} />
      </button>
      <Collapse open={open} id={bodyId}>
        <div className="mt-1.5 mb-1 border-l-2 border-border pl-4">
          <ChatMarkdown text={block.text} className="gap-2 text-[13px] leading-relaxed text-muted-foreground" />
        </div>
      </Collapse>
    </div>
  );
}

// ── Files ───────────────────────────────────────────────────

function iconFor(file: ChatFile) {
  const kind = fileKind(file);
  if (kind === 'PDF' || kind === 'Text' || kind === 'Markdown') return FileText;
  if (kind === 'CSV' || kind === 'Spreadsheet') return FileSpreadsheet;
  if (kind === 'Image') return FileImage;
  return FileIcon;
}

/** claude.ai's file cards: an icon by type, the name, "PDF · 2.1 MB" and Download; several in a grid. */
export function FileCards({ files, chatId }: { files: readonly ChatFile[]; chatId: string }) {
  if (!files.length) return null;
  return (
    <ul data-slot="chat-files" aria-label="Files it made" className="grid grid-cols-[repeat(auto-fill,minmax(min(100%,16rem),1fr))] gap-2">
      {files.map((file) => {
        const Icon = iconFor(file);
        const href = file.url || chatPaths.file(chatId, file.id);
        const meta = [fileKind(file), fileSize(file.size)].filter(Boolean).join(' · ');
        return (
          <li
            key={file.id}
            data-slot="chat-file"
            className="chat-rise flex min-w-0 items-center gap-3 rounded-xl border bg-card px-3 py-2.5 transition-colors hover:border-foreground/20"
          >
            <span className="grid size-10 shrink-0 place-items-center rounded-lg border bg-muted text-muted-foreground" aria-hidden="true">
              <Icon className="size-5" />
            </span>
            <span className="min-w-0 flex-1">
              <span className="block truncate text-sm font-medium" title={file.name}>{file.name}</span>
              <span className="block text-xs text-muted-foreground">{meta}</span>
            </span>
            <a
              href={href}
              download={file.name}
              aria-label={`Download ${file.name}`}
              className="inline-flex h-8 shrink-0 items-center gap-1.5 rounded-lg border bg-background px-2.5 text-sm font-medium transition-colors hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
            >
              <Download className="size-4" aria-hidden="true" />
              <span className="hidden min-[420px]:inline">Download</span>
            </a>
          </li>
        );
      })}
    </ul>
  );
}

// ── The marked-up PDF ──────────────────────────────────────

/**
 * "Download marked-up PDF": code draws clouds and notes from this answer's findings on a copy of the
 * upload (no AI call, the same every time). The new file appears as a card and downloads at once.
 */
function MarkupButton({ make, onMade }: { make: () => Promise<ChatFile>; onMade: (file: ChatFile) => void }) {
  const [state, setState] = useState<{ kind: 'idle' } | { kind: 'running' } | { kind: 'failed'; words: string }>({ kind: 'idle' });
  const running = state.kind === 'running';
  const click = () => {
    if (running) return;
    setState({ kind: 'running' });
    make().then(
      (file) => {
        setState({ kind: 'idle' });
        onMade(file);
      },
      (error: unknown) => setState({ kind: 'failed', words: markupFailureWords(error) }),
    );
  };
  return (
    <>
      <button
        data-slot="chat-markup"
        type="button"
        onClick={click}
        disabled={running}
        aria-busy={running}
        className={cn(EXPORT_BUTTON, 'disabled:cursor-progress disabled:opacity-70')}
      >
        {running ? <Spinner /> : <FileDown className="size-4" aria-hidden="true" />}
        {running ? 'Making the marked-up PDF…' : 'Download marked-up PDF'}
      </button>
      {state.kind === 'failed' && (
        <p role="alert" data-slot="chat-markup-error" className="inline-flex min-w-0 basis-full items-start gap-1.5 text-sm text-muted-foreground">
          <CircleAlert className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
          <span>{state.words}</span>
        </p>
      )}
    </>
  );
}

const EXPORT_BUTTON =
  'inline-flex h-8 items-center gap-1.5 rounded-lg border bg-background px-3 text-sm font-medium transition-colors hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none motion-reduce:transition-none';

// ── The answer ──────────────────────────────────────────────

/** #1548: the earlier answers' findings this answer corrected, one plain line each. */
function Corrections({ corrections }: { corrections: readonly ChatFindingUpdate[] }) {
  return (
    <div data-slot="chat-corrections" className="grid gap-1 rounded-lg border bg-muted/30 px-4 py-3 text-sm">
      <span className="text-xs font-medium text-muted-foreground">Corrections to earlier answers</span>
      <ul className="grid gap-1">
        {corrections.map((update) => (
          <li key={update.ref} data-ref={update.ref} className="min-w-0">
            <span className="font-medium tabular-nums">Answer {update.answer}, p.{update.finding.page}</span>{' '}
            {update.finding.item}:{' '}
            {update.dropped ? (
              <span>dropped ({update.dropped})</span>
            ) : (
              <span>
                vendor {update.finding.vendor}, architect {update.finding.architect}
              </span>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

function RetryButton({ onRetry, withWords = false }: { onRetry: () => void; withWords?: boolean }) {
  return (
    <button
      type="button"
      data-slot="chat-retry"
      onClick={onRetry}
      title="Retry"
      aria-label="Retry"
      className={cn(
        'inline-flex h-7 shrink-0 items-center gap-1.5 rounded-md text-xs text-muted-foreground transition-colors hover:bg-accent hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none',
        withWords ? 'border bg-background px-2.5 font-medium text-foreground' : 'px-1.5',
      )}
    >
      <RotateCw className="size-3.5" aria-hidden="true" />
      {withWords && <span aria-hidden="true">Retry</span>}
    </button>
  );
}

/**
 * What GV's sign does at the foot of a running answer (#1534): thinking while it thinks, turning
 * while a tool or helper runs, a gentle pulse while words stream in.
 */
function sparkState(blocks: readonly ViewBlock[], streaming: boolean): SparkState {
  const last = blocks[blocks.length - 1];
  if (streaming || (last?.type === 'text' && last.text.trim())) return 'streaming';
  if (last?.type === 'step') return 'working';
  return 'thinking';
}

/** One or two lines of words (no table or code): set with tight spacing. */
function isFewLines(text: string): boolean {
  const lines = text.split('\n').filter((line) => line.trim());
  return lines.length <= 2 && !lines.some((line) => /^\s*(\||```)/.test(line));
}

/** The server's answer to a decision: `{decision, note, by, at}`. */
function isDecision(value: unknown): value is FindingDecision {
  return Boolean(value) && typeof value === 'object' && ((value as FindingDecision).decision === 'agree' || (value as FindingDecision).decision === 'not_a_problem');
}

export interface AssistantMessageProps {
  message: ViewMessage;
  chatId: string;
  models: readonly ChatModel[] | null;
  /** No longer used: an answer names only the cap it was saved with (kept so callers need not change). */
  messageCapUsd?: string | null;
  /** Shown on a message whose stream broke: read the chat again to see what was saved. */
  onReload?: () => void;
  /**
   * Ask the question again as a new message (claude.ai's Retry). Optional: the page passes it when
   * it can send (`previousQuestion` in `chat-messages` finds the question).
   */
  onRetry?: () => void;
  /**
   * #1514: go on from an answer a server restart interrupted ("Continue from where you stopped").
   * The page passes it on the newest interrupted answer when it can send.
   */
  onContinue?: () => void;
  /** The newest answer: its Copy and Retry stay shown; on older answers they appear on hover. */
  latest?: boolean;
  /**
   * Chat v2: make the marked-up PDF of this answer's findings (`POST …/markup-pdf`). The button
   * shows on a finished answer with findings when this is given.
   */
  makeMarkupPdf?: (messageId: string) => Promise<ChatFile>;
  /** Chat v2: where the evidence viewer's "Open in review screen" goes; left out when absent. */
  fullPageHref?: string | null;
  /** #1717: the vendor's page picture (the set pack's, 100 dpi), for the evidence panel's boxed spot. */
  pagePicture?: ((page: number) => string) | null;
  /**
   * Chat v4: the answer to a full check. Since #1508 it renders as the work fold → the summary strip
   * (Wrong · To check · Right · Not compared · Pages · Accuracy) → the cloud drawing (large) and the
   * check report → What's wrong / To check / What's right, grouped by area → the AI notes → the
   * trust summary → coverage.
   */
  fullCheck?: boolean;
  /** #1522 (PATH step 5): record the reviewer's Agree / Not a problem on a finding (#1803: with its note and the value it should be). */
  decide?: (messageId: string, findingId: string, decision: FindingDecisionChoice, extra?: FindingDecisionExtra) => Promise<unknown>;
  /** #1525: make the cloud drawing again from the reviewer's decisions; returns the new drawing (#1803: and every file made with it). */
  remakeCloud?: (messageId: string) => Promise<RemadeFiles>;
  /** #1578: a finding of this answer to open in the evidence viewer at once (the job tracker's link). */
  focusFindingId?: string | null;
  /** #1727: Stop on the live full check card (with its confirmation), for the answer running now. */
  liveStop?: LiveStopControls | null;
}

/** #1727: the live card's Stop: the page's stop, whether it is stopping, and the confirmation's state. */
export interface LiveStopControls {
  onStop: () => void;
  stopping: boolean;
  askStop: boolean;
  onAskStop: (open: boolean) => void;
}

export function AssistantMessage({ message, chatId, models, onReload, onRetry, onContinue, latest = false, makeMarkupPdf, fullPageHref, pagePicture = null, fullCheck = false, decide, remakeCloud, focusFindingId = null, liveStop = null }: AssistantMessageProps) {
  const running = message.status === 'running';
  // #1727: the live card while a full check runs (from the first moment: getting ready). An older
  // server that sends only `progress` keeps the one calm line instead.
  const live = fullCheck && running ? shownLive(message) ?? (message.progress ? null : startingLive(message.created_at)) : null;
  // When the check ends the card folds away (320 ms) before the result comes in.
  const reduced = useReducedMotion();
  const [wasRunning, setWasRunning] = useState(running);
  const [folding, setFolding] = useState(false);
  if (wasRunning !== running) {
    setWasRunning(running);
    if (!running && fullCheck && shownLive(message) && !reduced) setFolding(true);
  }
  useEffect(() => {
    if (!folding) return;
    const timer = window.setTimeout(() => setFolding(false), 320);
    return () => window.clearTimeout(timer);
  }, [folding]);
  const liveShown = live ?? (folding ? shownLive(message) : null);
  const pictureUrl = (pictureId: string) => chatPaths.picture(chatId, pictureId);
  const [filter, setFilter] = useState<FindingFilter>(NO_FILTER);
  const [viewing, setViewing] = useState<string | null>(focusFindingId);
  // #1578: a link from the job tracker opens its finding, and brings the answer into view behind it.
  const [focused, setFocused] = useState(focusFindingId);
  if (focused !== focusFindingId) {
    setFocused(focusFindingId);
    if (focusFindingId) setViewing(focusFindingId);
  }
  const articleRef = useRef<HTMLElement>(null);
  useEffect(() => {
    if (focusFindingId) articleRef.current?.scrollIntoView?.({ block: 'start' });
  }, [focusFindingId]);
  const [madeFiles, setMadeFiles] = useState<ChatFile[]>([]);
  const baseId = useId();
  const sectionIds = { different: `${baseId}-wrong`, check: `${baseId}-decide`, same: `${baseId}-right` };
  const findings = message.findings ?? [];
  const coverage = message.coverage ?? null;
  // A one- or two-line answer with no work, findings or files: just its words and the meta line.
  const short = isShortAnswer(message);
  const blocks = shownBlocks(message);
  const { items: allItems } = answerItems(blocks, Boolean(message.tasks?.length));
  const items = short ? allItems.filter((item) => item.kind === 'text') : allItems;
  // #1522: a narrow answer (a few pages) shows no whole-set panels: no page grid, no coverage, no ledger.
  const narrow = isNarrowAnswer(message, fullCheck);
  const allFiles = [...message.files, ...madeFiles.filter((made) => !message.files.some((file) => file.id === made.id))];
  const made = fullCheck ? fullCheckFiles(allFiles) : { cloud: null, report: null, notCompared: [], pkg: null, rest: allFiles };
  const files = made.rest;
  const messageId = message.id;
  const finished = !running && message.status === 'done' && findings.length > 0;
  // A full check's cloud drawing already is the marked-up PDF.
  const canMarkup = finished && messageId !== null && makeMarkupPdf !== undefined && !made.cloud;
  const showTrust = (findings.length > 0 && (hasTrust(findings, message.trust) || roundCounts(findings, message.trust) !== null)) || (message.unknowns?.length ?? 0) > 0;
  // ONE coverage display: the compact line (grid on expand) inside "How this was checked", whole-set answers only.
  const coverageStrip = coverage && !narrow ? (
    <CoverageStrip
      coverage={coverage}
      page={filter.page}
      onPickPage={findings.length ? (page) => setFilter({ ...filter, page: filter.page === page ? null : page }) : undefined}
    />
  ) : null;
  const streaming = textStreaming(message);
  const stopped = stoppedWords(message);
  const answerText = (message.stopped ? withoutStopNote(message.text) : message.text)
    || blocks.filter((block) => block.type === 'text').map((block) => block.text).join('\n\n');
  const retry = !running && onRetry ? onRetry : undefined;

  // PATH step 5: Agree / Not a problem (optimistic; deciding again is the undo) on every finished
  // answer. A server without the route (404 / 405) hides the buttons again and says so once.
  // A finished full check (#1684) counts as its screen shows: every Wrong and every Nothing to compare
  // (a "same" seen on one side only is Not compared, and can be made Wrong too).
  const actionable = fullCheck && !running ? findings.filter((finding) => proofGroup(finding, null) !== 'right') : findings.filter((finding) => finding.status !== 'same');
  // A finding's entry is its newest decision on this screen; `null` after an Undo (#1803: what code found).
  const [decisions, setDecisions] = useState<Record<string, FindingDecision | null>>({});
  const [decideFailed, setDecideFailed] = useState<string | null>(null);
  const [decisionsOff, setDecisionsOff] = useState(false);
  const decisionOf = (finding: ChatFinding): FindingDecision | null => (finding.id in decisions ? decisions[finding.id] : finding.decision) ?? null;
  // #1619: a finished full check shows each item where its cloud drawing puts it (Wrong = proved
  // by code, Right, Nothing to compare), the reviewer's newest click laid over.
  const architectView = fullCheck && !running;
  const shown = architectView ? findings.map((finding) => asShown(finding, decisionOf(finding))) : findings;
  // The viewer's arrows step through the findings in the order the sections show them.
  const ordered = architectView ? (() => {
    const groups = resultGroups(findings, decisionOf);
    return [...groups.wrong, ...groups.notCompared, ...groups.dismissed, ...groups.right];
  })() : sectionOrder(shown, filter);
  // #1803: after each saved decision of a full check, the draft (cloud drawing, letter, register,
  // Not-compared list, package) is made again by itself, a moment later so quick decisions share one
  // remake; decisions saved while it runs make it once more after.
  // A remake done on this screen answers the server's flag until the chat is read again.
  const filesKey = message.files.map((file) => file.id).join(',');
  const [remadeKey, setRemadeKey] = useState<string | null>(null);
  const [remakeWanted, setRemakeWanted] = useState(0);
  const [remakeDone, setRemakeDone] = useState(0);
  const [remakeRunning, setRemakeRunning] = useState(false);
  const [remakeFailed, setRemakeFailed] = useState(false);
  const [draftAt, setDraftAt] = useState<Date | null>(null);
  // The page's functions change on every render; the timer reads the newest ones.
  const latestRemake = useRef({ remakeCloud, onReload, filesKey });
  useEffect(() => { latestRemake.current = { remakeCloud, onReload, filesKey }; });
  const canRemake = Boolean(remakeCloud && messageId);
  useEffect(() => {
    if (!canRemake || !messageId || remakeWanted === remakeDone || remakeRunning || remakeFailed) return undefined;
    const target = remakeWanted;
    const timer = setTimeout(() => {
      const { remakeCloud: remake, onReload: reload } = latestRemake.current;
      if (!remake) return;
      setRemakeRunning(true);
      remake(messageId).then(
        (file) => {
          // #1803: every file made with the drawing (the package too), so the new package is offered at once, never the old one.
          const fresh = file.made?.length ? file.made : [file];
          setMadeFiles((known) => [...known, ...fresh.filter((made) => !known.some((each) => each.id === made.id))]);
          setRemadeKey(latestRemake.current.filesKey);
          // The new letter, register, list and package are added to the message's files on the server: read the chat again.
          reload?.();
          setDraftAt(new Date());
          setRemakeDone(target);
        },
        () => setRemakeFailed(true),
      ).finally(() => setRemakeRunning(false));
    }, REMAKE_DELAY_MS);
    return () => clearTimeout(timer);
  }, [canRemake, messageId, remakeWanted, remakeDone, remakeRunning, remakeFailed]);
  const askRemake = () => { setRemakeFailed(false); setRemakeWanted((count) => count + 1); };
  const cloudState: 'idle' | 'updating' | 'failed' = remakeFailed ? 'failed' : remakeRunning || remakeWanted !== remakeDone ? 'updating' : 'idle';
  // #1803: the server says the package was made before the newest decision (a decision made
  // elsewhere, or a remake that never ran): it is never handed out, and it is made again at once.
  const outdated = Boolean(fullCheck && finished && message.package_outdated) && remadeKey !== filesKey;
  useEffect(() => {
    if (!outdated || !canRemake || remakeWanted !== remakeDone || remakeRunning || remakeFailed) return undefined;
    const timer = setTimeout(askRemake, 0);
    return () => clearTimeout(timer);
    // Only when the server's answer changes: a remake already asked for is not asked again.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [outdated, canRemake]);
  // The package on the screen does not hold every decision yet: no download until it does.
  // #1803: stale from the click on (a decision still being saved), not only once it is saved.
  const [saving, setSaving] = useState(0);
  const packageStale = cloudState !== 'idle' || outdated || saving > 0;
  const decisionFor = finished && !decisionsOff && decide && messageId ? (finding: ChatFinding): DecisionProps => ({
    decision: decisionOf(finding)?.decision ?? null,
    saved: decisionOf(finding),
    onDecide: (decision, extra) => {
      const had = finding.id in decisions;
      const before = decisions[finding.id];
      setDecideFailed(null);
      const laid: FindingDecision | null = decision === 'undo' ? null : { ...(decisionOf(finding) ?? {}), decision, note: extra?.note?.trim() || null, should_be: decision === 'agree' ? extra?.should_be?.trim() || null : null };
      setDecisions((now) => ({ ...now, [finding.id]: laid }));
      setSaving((count) => count + 1);
      (extra ? decide(messageId, finding.id, decision, extra) : decide(messageId, finding.id, decision)).then(
        (saved) => {
          setDecisions((now) => ({ ...now, [finding.id]: decision !== 'undo' && isDecision(saved) ? saved : laid }));
          if (fullCheck && remakeCloud) askRemake();
        },
        (error: unknown) => {
          const status = error instanceof ApiError ? error.status : null;
          if (status === 404 || status === 405) setDecisionsOff(true);
          setDecideFailed(status === 404 || status === 405 ? 'Answers cannot be saved on this server yet.'
            : status === 409 ? 'Wait until the answer has finished, then try again.'
            : 'Your answer was not saved. Try again.');
          setDecisions((now) => {
            const next = { ...now };
            if (had) next[finding.id] = before;
            else delete next[finding.id];
            return next;
          });
        },
      ).finally(() => setSaving((count) => count - 1));
    },
  }) : null;
  // #1525: "12 of 43 decided", and the cloud drawing made again from the answers.
  const decidedCount = actionable.filter((finding) => decisionOf(finding)).length;
  const deciders = [...new Set(actionable.map((finding) => decisionOf(finding)?.by).filter((by): by is string => Boolean(by)))];
  const updateCloud = decisionFor && remakeCloud && messageId ? askRemake : null;
  const review: CloudReview | null = decisionFor ? {
    decided: decidedCount,
    total: actionable.length,
    reviewedBy: decidedCount === actionable.length && deciders.length ? deciders.join(', ') : null,
    onUpdate: updateCloud,
    updating: cloudState === 'updating',
    failed: cloudState === 'failed',
    updatedAt: draftAt ? clockWords(draftAt) : null,
    stale: packageStale,
  } : null;

  // The task list sits inside the activity timeline, at its newest update (the last task step);
  // the list from the newest `tasks` event wins over the one that step carried.
  const lastTaskAt = blocks.reduce((last, block, at) => (block.type === 'step' && (block.step.tool === 'TaskCreate' || block.step.tool === 'TaskUpdate') ? at : last), -1);
  const lastTaskStep = lastTaskAt >= 0 ? (blocks[lastTaskAt] as Extract<ViewBlock, { type: 'step' }>) : null;
  const tasks = message.tasks?.length ? message.tasks : lastTaskStep?.step.tasks ?? null;
  const looseTasks = tasks?.length && lastTaskAt < 0 ? <TaskChecklist tasks={tasks} running={running} /> : null;

  const renderItem = (item: (typeof items)[number], at: number) => {
    const last = at === items.length - 1;
    return (
      <div key={item.kind === 'activity' ? `activity-${item.indexes[0]}` : `text-${item.index}`} className="grid min-w-0 gap-3">
        {item.kind === 'activity' && (
          item.indexes.length === 1 && blocks[item.indexes[0]]?.type === 'thinking' ? (
            <ThinkingBlock block={blocks[item.indexes[0]] as Extract<ViewBlock, { type: 'thinking' }>} />
          ) : (
            <ActivityRow
              blocks={item.indexes.map((index) => blocks[index])}
              active={running && last}
              pictureUrl={pictureUrl}
              tasks={tasks}
              tasksAt={item.indexes.indexOf(lastTaskAt) >= 0 ? item.indexes.indexOf(lastTaskAt) : null}
              running={running}
            />
          )
        )}
        {item.kind === 'text' && (
          <ChatMarkdown
            text={(blocks[item.index] as Extract<ViewBlock, { type: 'text' }>).text}
            streaming={streaming && last && item.index === blocks.length - 1}
            // A short answer's lines sit close together; a long answer keeps its reading rhythm.
            className={cn('max-w-[46rem]', short || isFewLines((blocks[item.index] as Extract<ViewBlock, { type: 'text' }>).text) ? 'gap-2' : undefined)}
          />
        )}
      </div>
    );
  };
  const sections = findings.length > 0 ? (
    <FindingsSections
      findings={shown}
      architectView={architectView}
      filter={filter}
      onFilter={setFilter}
      onOpen={(finding) => setViewing(finding.id)}
      pictureUrl={pictureUrl}
      fullPageHref={fullPageHref}
      decisionFor={decisionFor}
      narrow={narrow}
      ids={sectionIds}
      resolver={message.resolver ?? null}
    />
  ) : null;
  const decideError = decideFailed ? (
    <p role="alert" data-slot="chat-decision-error" className="inline-flex items-center gap-1.5 text-sm text-muted-foreground">
      <CircleAlert className="size-4 shrink-0" aria-hidden="true" />
      {decideFailed}
    </p>
  ) : null;
  const howChecked = showTrust && !narrow ? (
    <HowChecked findings={findings} trust={message.trust} coverage={coverage} coverageStrip={coverageStrip} unknowns={message.unknowns} />
  ) : null;
  const hasResult = fullCheck && (findings.length > 0 || coverage !== null || made.cloud !== null || made.report !== null);
  const resultShown = hasResult && !running && !folding;
  const textItems = items.filter((item) => item.kind === 'text');

  return (
    <article
      ref={articleRef}
      data-slot="chat-assistant-message"
      data-status={message.status}
      data-short={short ? 'true' : undefined}
      data-narrow={findings.length && narrow ? 'true' : undefined}
      aria-label="Answer"
      className={cn('group/message chat-rise flex min-w-0 flex-col', short ? 'gap-1' : 'gap-3')}
    >
      {(fullCheck ? (resultShown || liveShown ? [] : items.filter((item) => item.kind === 'activity')) : items).map((item) => renderItem(item, items.indexOf(item)))}
      {liveShown && (
        <div data-slot="chat-live-fold" data-shut={folding ? 'true' : 'false'} className="chat-live-fold chat-wide min-w-0">
          <div className="min-h-0 min-w-0 overflow-hidden">
            <LiveCheck
              live={liveShown}
              eta={liveShown.phase.phase === 'comparing' ? message.progress?.eta_seconds ?? null : null}
              files={message.files}
              model={shortModelWords(message.model, models)}
              cost={messageCost(message.cost_usd)}
              pagePicture={pagePicture}
              onStop={running && liveStop ? liveStop.onStop : undefined}
              stopping={liveStop?.stopping ?? false}
              askStop={liveStop?.askStop ?? false}
              onAskStop={liveStop?.onAskStop}
            />
          </div>
        </div>
      )}
      {fullCheck && running && !live && <div className="chat-wide grid min-w-0"><FullCheckProgress message={message} /></div>}
      {resultShown && (
        <div data-slot="chat-full-check-lead" className="chat-wide grid min-w-0 gap-7">
          {/* #1717: summary first, each number in one place, the evidence beside each item. */}
          <FullCheckResult
            findings={findings}
            decisionOf={decisionOf}
            chatId={chatId}
            files={{ cloud: made.cloud, report: made.report, notCompared: made.notCompared, pkg: made.pkg }}
            reviewAction={message.review_action ?? null}
            packageState={message.package_state ?? null}
            work={{
              drawingsDone: message.progress?.drawings_done ?? null,
              drawingsTotal: message.progress?.drawings_total ?? null,
              pagesTotal: message.progress?.pages_total || (coverage ? coverageCounts(coverage).total : null),
              seconds: message.trust?.timings?.total_seconds ?? null,
              stopped: message.stopped === 'cancelled',
              timings: message.trust?.timings ?? null,
            }}
            pictureUrl={pictureUrl}
            onOpen={setViewing}
            onRunAgain={retry ?? null}
            review={review}
            rounds={roundCounts(findings, message.trust)}
            activity={items.some((item) => item.kind === 'activity') ? <div className="grid min-w-0 gap-3">{items.filter((item) => item.kind === 'activity').map((item) => renderItem(item, items.indexOf(item)))}</div> : null}
            summary={textItems.length ? (
              <div className="grid min-w-0 gap-2">
                {textItems.map((item) => {
                  const block = blocks[item.index] as Extract<ViewBlock, { type: 'text' }>;
                  return <ChatMarkdown key={`summary-${item.index}`} text={block.by === 'code' ? withoutCodeNote(block.text).text : block.text} />;
                })}
              </div>
            ) : null}
          />
          {decideError}
          {/* PATH 4.7: everything else, folded. */}
          {howChecked}
        </div>
      )}
      {fullCheck && !resultShown && textItems.map((item) => renderItem(item, items.indexOf(item)))}
      {looseTasks}

      {(message.cites?.length ?? 0) > 0 && <CiteChips cites={message.cites ?? []} pictureUrl={pictureUrl} />}

      {!fullCheck && sections && <div className="chat-wide grid min-w-0 gap-3">{decideError}{sections}</div>}
      {!fullCheck && findings.length > 0 && narrow && <ConfidenceLine findings={findings} trust={message.trust} />}
      {!fullCheck && howChecked}
      {!fullCheck && !howChecked && coverageStrip}
      {findings.length > 0 && (
        <EvidenceViewer
          findings={ordered}
          openId={viewing}
          onOpen={setViewing}
          pictureUrl={pictureUrl}
          fullPageHref={resultShown ? null : fullPageHref}
          pagePicture={pagePicture}
          cloudAt={resultShown && made.cloud ? () => openHref(made.cloud!, made.cloud!.url || chatPaths.file(chatId, made.cloud!.id)) : null}
          decisionFor={resultShown ? decisionFor : null}
          numberOf={resultShown ? (finding) => commentNumber(finding, message.package_state) : null}
          notice={decideFailed}
          draft={resultShown && review ? { updating: review.updating, failed: review.failed, updatedAt: review.updatedAt, stale: review.stale ?? false, decided: review.decided, pkg: made.pkg, href: made.pkg ? made.pkg.url || chatPaths.file(chatId, made.pkg.id) : null, onRetry: review.onUpdate } : null}
        />
      )}
      {finished && !resultShown && (
        <div data-slot="chat-exports" className="flex flex-wrap items-center gap-x-2 gap-y-1.5">
          {canMarkup && (
            <MarkupButton
              make={() => makeMarkupPdf(messageId)}
              onMade={(file) => {
                setMadeFiles((known) => (known.some((made) => made.id === file.id) ? known : [...known, file]));
                downloadFile(file.url || chatPaths.file(chatId, file.id), file.name);
              }}
            />
          )}
          <button
            type="button"
            data-slot="chat-csv"
            onClick={() => downloadFindingsCsv(findings, `findings-${(messageId ?? 'answer').slice(0, 8)}.csv`)}
            className={EXPORT_BUTTON}
          >
            <Sheet className="size-4" aria-hidden="true" />
            Download Excel (CSV)
          </button>
        </div>
      )}

      <FileCards files={files} chatId={chatId} />

      {stopped && message.stopped === 'interrupted' && (
        <div data-slot="chat-stopped" data-stopped="interrupted" className="grid min-w-0 gap-2 text-sm">
          <span className="inline-flex min-w-0 items-start gap-2 font-medium">
            <CircleAlert className="mt-0.5 size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
            <span>{stopped}</span>
          </span>
          {message.resume?.redo && <p data-slot="chat-resume-redo" className="text-muted-foreground">{message.resume.redo}</p>}
          {!running && onContinue && (
            <div>
              <button
                type="button"
                data-slot="chat-continue"
                onClick={onContinue}
                className="inline-flex min-h-8 items-center gap-1.5 rounded-lg border bg-background px-3 text-sm font-medium hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
              >
                <Play className="size-3.5" aria-hidden="true" />
                Continue
              </button>
            </div>
          )}
        </div>
      )}

      {stopped && message.stopped !== 'error' && message.stopped !== 'interrupted' && !resultShown && (
        <p data-slot="chat-stopped" data-stopped={message.stopped ?? undefined} className="text-sm text-muted-foreground">{stopped}</p>
      )}

      {stopped && message.stopped === 'error' && (
        <div data-slot="chat-stopped" data-stopped="error" className="flex flex-wrap items-center gap-x-3 gap-y-2 text-sm">
          <span className="inline-flex min-w-0 items-start gap-2">
            <CircleAlert className="mt-0.5 size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
            <span>{stopped}</span>
          </span>
          {retry && <RetryButton onRetry={retry} withWords />}
        </div>
      )}

      {message.status === 'failed' && (
        <div data-slot="chat-failed" className="flex flex-wrap items-center gap-x-3 gap-y-2 text-sm">
          <span className="inline-flex min-w-0 items-start gap-2">
            <CircleAlert className="mt-0.5 size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
            <span>{message.failure ?? 'The answer stopped before it was complete.'}</span>
          </span>
          {onReload && (
            <button
              type="button"
              onClick={onReload}
              className="inline-flex min-h-8 items-center gap-1.5 rounded-lg border bg-background px-3 text-sm font-medium hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
            >
              <RotateCw className="size-3.5" aria-hidden="true" />
              Reload chat
            </button>
          )}
        </div>
      )}

      {(message.corrections?.length ?? 0) > 0 && <Corrections corrections={message.corrections ?? []} />}

      {/* #1577: Anthropic's refusal fallback wrote this answer: said once, quietly. */}
      {message.fallback && (
        <p data-slot="chat-fallback" className="flex min-w-0 items-start gap-1.5 text-xs leading-snug text-muted-foreground">
          <ArrowRightLeft className="mt-px size-3.5 shrink-0" aria-hidden="true" />
          <span>{fallbackWords(message.fallback, models)}</span>
        </p>
      )}

      {running && !live && <Spark label={message.id ? 'Working' : 'Sending your message'} state={sparkState(blocks, streaming)} className="chat-fade-in" />}

      {!running && (
        <div data-slot="chat-footer" className="flex min-w-0 flex-wrap items-center justify-between gap-x-3 gap-y-1">
          <div
            data-slot="chat-actions"
            className={cn(
              'flex min-h-7 items-center gap-0.5 transition-opacity duration-150 focus-within:opacity-100 group-hover/message:opacity-100 [@media(hover:none)]:opacity-100 motion-reduce:transition-none',
              latest && !short ? 'opacity-100' : 'opacity-0',
            )}
          >
            {answerText.trim() && <CopyButton text={answerText} />}
            {retry && <RetryButton onRetry={retry} />}
          </div>
          <span className="flex min-w-0 flex-wrap items-center justify-end gap-x-3 gap-y-1">
            <span data-slot="chat-meta" className="min-w-0 text-xs text-muted-foreground">{footerWords(message, models)}</span>
            {/* Chat v5: code's accuracy closes every answer that has one (older answers show none). */}
            {message.accuracy && <AccuracyBadge accuracy={message.accuracy} />}
          </span>
        </div>
      )}
    </article>
  );
}

/** #1803: how long after a decision the draft is made again (quick decisions share one remake). */
const REMAKE_DELAY_MS = 1200;

/** "14:32": the time the draft was made again, on the reviewer's clock. */
function clockWords(at: Date): string {
  return at.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}
