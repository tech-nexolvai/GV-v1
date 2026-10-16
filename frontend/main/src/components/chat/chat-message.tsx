import { useId, useState } from 'react';
import { ChevronRight, CircleAlert, Download, FileDown, Sheet, FileImage, FileSpreadsheet, FileText, File as FileIcon, RotateCw, ScanSearch } from 'lucide-react';

import { AccuracyBadge, CiteChips } from '@/components/chat/chat-accuracy';
import { ActivityRow, TaskChecklist } from '@/components/chat/chat-activity';
import { CoverageStrip } from '@/components/chat/chat-coverage';
import { EvidenceViewer } from '@/components/chat/chat-evidence';
import { FindingsTable } from '@/components/chat/chat-findings';
import { FullCheckFiles } from '@/components/chat/chat-full-check';
import { TrustSummary } from '@/components/chat/chat-trust';
import { ChatMarkdown } from '@/components/chat/chat-markdown';
import { Collapse, CopyButton, Shimmer, Spark, Spinner } from '@/components/chat/chat-motion';
import { chatPaths, type ChatEffort, type ChatFile, type ChatModel } from '@/lib/chat-api';
import { findingCounts, groupFindings, isShortAnswer, NO_FILTER, shownInOrder, type FindingFilter } from '@/lib/chat-findings';
import { fullCheckBubble, fullCheckFiles } from '@/lib/chat-full-check';
import { EFFORT_WORDS } from '@/lib/chat-settings';
import { downloadFile, downloadFindingsCsv, markupFailureWords } from '@/lib/chat-exports';
import { hasTrust, roundCounts } from '@/lib/chat-trust';
import {
  answerItems,
  fileKind,
  fileSize,
  footerWords,
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

/** One or two lines of words (no table or code): set with tight spacing. */
function isFewLines(text: string): boolean {
  const lines = text.split('\n').filter((line) => line.trim());
  return lines.length <= 2 && !lines.some((line) => /^\s*(\||```)/.test(line));
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
  /** The newest answer: its Copy and Retry stay shown; on older answers they appear on hover. */
  latest?: boolean;
  /**
   * Chat v2: make the marked-up PDF of this answer's findings (`POST …/markup-pdf`). The button
   * shows on a finished answer with findings when this is given.
   */
  makeMarkupPdf?: (messageId: string) => Promise<ChatFile>;
  /** Chat v2: where the evidence viewer's "Open in review screen" goes; left out when absent. */
  fullPageHref?: string | null;
  /**
   * Chat v4: the answer to a full check. It renders as AI notes → trust summary → the cloud drawing
   * (first and large) and the check report → What's wrong / To check / What's right → coverage.
   */
  fullCheck?: boolean;
}

export function AssistantMessage({ message, chatId, models, onReload, onRetry, latest = false, makeMarkupPdf, fullPageHref, fullCheck = false }: AssistantMessageProps) {
  const running = message.status === 'running';
  const pictureUrl = (pictureId: string) => chatPaths.picture(chatId, pictureId);
  const [filter, setFilter] = useState<FindingFilter>(NO_FILTER);
  const [viewing, setViewing] = useState<string | null>(null);
  const [madeFiles, setMadeFiles] = useState<ChatFile[]>([]);
  const findings = message.findings ?? [];
  const coverage = message.coverage ?? null;
  // A one- or two-line answer with no work, findings or files: just its words and the meta line.
  const short = isShortAnswer(message);
  const blocks = shownBlocks(message);
  const { items: allItems } = answerItems(blocks, Boolean(message.tasks?.length));
  const items = short ? allItems.filter((item) => item.kind === 'text') : allItems;
  // A full check opens on its first tab that has rows (What's wrong, else To check, else What's right).
  const counts = findingCounts(findings);
  const opening = counts.different ? 'different' : counts.check ? 'check' : 'same';
  const shownFilter: FindingFilter = fullCheck && filter.status === 'all' ? { ...filter, status: opening } : filter;
  // The viewer's arrows step through the findings in the table's order, under its filter.
  const ordered = shownInOrder(groupFindings(findings, shownFilter));
  const allFiles = [...message.files, ...madeFiles.filter((made) => !message.files.some((file) => file.id === made.id))];
  const made = fullCheck ? fullCheckFiles(allFiles) : { cloud: null, report: null, rest: allFiles };
  const files = made.rest;
  const messageId = message.id;
  const finished = !running && message.status === 'done' && findings.length > 0;
  // A full check's cloud drawing already is the marked-up PDF.
  const canMarkup = finished && messageId !== null && makeMarkupPdf !== undefined && !made.cloud;
  const showTrust = findings.length > 0 && (hasTrust(findings, message.trust) || roundCounts(findings, message.trust) !== null);
  // The coverage strip sits in the trust summary when there is one, else on its own under the table.
  const coverageStrip = coverage ? (
    <CoverageStrip
      coverage={coverage}
      page={filter.page}
      onPickPage={findings.length ? (page) => setFilter({ ...filter, status: fullCheck ? shownFilter.status : 'all', page: filter.page === page ? null : page }) : undefined}
    />
  ) : null;
  const streaming = textStreaming(message);
  const stopped = stoppedWords(message);
  const answerText = (message.stopped ? withoutStopNote(message.text) : message.text)
    || blocks.filter((block) => block.type === 'text').map((block) => block.text).join('\n\n');
  const retry = !running && onRetry ? onRetry : undefined;
  // The task list sits inside the activity timeline, at its newest update (the last task step);
  // the list from the newest `tasks` event wins over the one that step carried.
  const lastTaskAt = blocks.reduce((last, block, at) => (block.type === 'step' && (block.step.tool === 'TaskCreate' || block.step.tool === 'TaskUpdate') ? at : last), -1);
  const lastTaskStep = lastTaskAt >= 0 ? (blocks[lastTaskAt] as Extract<ViewBlock, { type: 'step' }>) : null;
  const tasks = message.tasks?.length ? message.tasks : lastTaskStep?.step.tasks ?? null;
  const looseTasks = tasks?.length && lastTaskAt < 0 ? <TaskChecklist tasks={tasks} running={running} /> : null;

  return (
    <article
      data-slot="chat-assistant-message"
      data-status={message.status}
      data-short={short ? 'true' : undefined}
      aria-label="Answer"
      className={cn('group/message chat-rise flex min-w-0 flex-col', short ? 'gap-1' : 'gap-3')}
    >
      {items.map((item, at) => {
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
                className={short || isFewLines((blocks[item.index] as Extract<ViewBlock, { type: 'text' }>).text) ? 'gap-2' : undefined}
              />
            )}
          </div>
        );
      })}
      {looseTasks}

      {(message.cites?.length ?? 0) > 0 && <CiteChips cites={message.cites ?? []} pictureUrl={pictureUrl} />}

      {showTrust && <TrustSummary findings={findings} trust={message.trust} coverage={coverage} coverageStrip={fullCheck ? null : coverageStrip} />}

      {fullCheck && <FullCheckFiles cloud={made.cloud} report={made.report} chatId={chatId} />}

      {findings.length > 0 && (
        <FindingsTable findings={findings} filter={shownFilter} onFilter={setFilter} onOpen={(finding) => setViewing(finding.id)} pictureUrl={pictureUrl} tabs={fullCheck} />
      )}
      {findings.length > 0 && (
        <EvidenceViewer findings={ordered} openId={viewing} onOpen={setViewing} pictureUrl={pictureUrl} fullPageHref={fullPageHref} />
      )}

      {coverage && (fullCheck || !showTrust) && coverageStrip}

      {finished && (
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

      {stopped && message.stopped !== 'error' && (
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

      {running && <Spark label={message.id ? 'Working' : 'Sending your message'} className="chat-fade-in" />}

      {!running && (
        <div data-slot="chat-footer" className="-ml-1.5 flex min-w-0 flex-wrap items-center justify-between gap-x-3 gap-y-1">
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
