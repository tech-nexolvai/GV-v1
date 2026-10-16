import { useId, useState } from 'react';
import { ChevronRight, CircleAlert, Download, FileImage, FileSpreadsheet, FileText, File as FileIcon, RotateCw } from 'lucide-react';

import { ActivityRow, TaskChecklist } from '@/components/chat/chat-activity';
import { ChatMarkdown } from '@/components/chat/chat-markdown';
import { Collapse, CopyButton, Shimmer, Spark } from '@/components/chat/chat-motion';
import { chatPaths, type ChatFile, type ChatModel } from '@/lib/chat-api';
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

export function UserMessage({ message }: { message: ViewMessage }) {
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

export interface AssistantMessageProps {
  message: ViewMessage;
  chatId: string;
  models: readonly ChatModel[] | null;
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
}

export function AssistantMessage({ message, chatId, models, messageCapUsd, onReload, onRetry, latest = false }: AssistantMessageProps) {
  const running = message.status === 'running';
  const pictureUrl = (pictureId: string) => chatPaths.picture(chatId, pictureId);
  const blocks = shownBlocks(message);
  const { items } = answerItems(blocks, Boolean(message.tasks?.length));
  const streaming = textStreaming(message);
  const stopped = stoppedWords(message, messageCapUsd);
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
    <article data-slot="chat-assistant-message" data-status={message.status} aria-label="Answer" className="group/message chat-rise flex min-w-0 flex-col gap-3">
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
              />
            )}
          </div>
        );
      })}
      {looseTasks}

      <FileCards files={message.files} chatId={chatId} />

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
              latest ? 'opacity-100' : 'opacity-0',
            )}
          >
            {answerText.trim() && <CopyButton text={answerText} />}
            {retry && <RetryButton onRetry={retry} />}
          </div>
          <span data-slot="chat-meta" className="min-w-0 text-xs text-muted-foreground">{footerWords(message, models)}</span>
        </div>
      )}
    </article>
  );
}
