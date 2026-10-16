import { useEffect, useId, useState } from 'react';
import { Check, ChevronRight, CircleAlert, Copy, Download, FileImage, FileSpreadsheet, FileText, File as FileIcon, RotateCw } from 'lucide-react';

import { AssistantSteps, LiveSteps, PlanList, StepItem } from '@/components/assistant/assistant-steps';
import { ChatMarkdown } from '@/components/chat/chat-markdown';
import { chatPaths, type ChatFile, type ChatModel } from '@/lib/chat-api';
import {
  answerItems,
  fileKind,
  fileSize,
  footerWords,
  somethingLive,
  stoppedWords,
  textStreaming,
  thoughtFor,
  toAssistantStep,
  toPlanTasks,
  type ViewBlock,
  type ViewMessage,
  type ViewStep,
} from '@/lib/chat-messages';
import type { LiveAgent } from '@/lib/assistant-steps';
import { cn } from '@/lib/utils';

/**
 * One message of a chat (#1464). The user's question sits on the right; the answer takes the full
 * width and shows, in the order it happened: "Thought for 12s" blocks, the tools it used (the
 * assistant panel's step list), its task list, helper agents, the markdown answer, the files it
 * made, and a footer naming the model, effort and cost. Read-only: nothing here changes the review.
 */

export function UserMessage({ message }: { message: ViewMessage }) {
  return (
    <div data-slot="chat-user-message" className="max-w-[85%] self-end rounded-[18px_18px_4px_18px] bg-muted px-4 py-2.5 leading-relaxed break-words whitespace-pre-wrap">
      {message.text}
    </div>
  );
}

// ── Thinking ────────────────────────────────────────────────

function Spinner() {
  return <span aria-hidden="true" className="size-3.5 shrink-0 animate-spin rounded-full border-[1.5px] border-border border-t-foreground motion-reduce:animate-none" />;
}

/**
 * A thinking block: open while its summary streams in, folded to "Thought for 12s" once it ends.
 * After the reviewer opens or folds it themselves, it stays the way they left it.
 */
export function ThinkingBlock({ block }: { block: Extract<ViewBlock, { type: 'thinking' }> }) {
  const [chosen, setChosen] = useState<boolean | null>(null);
  const open = chosen ?? block.live;
  const bodyId = useId();
  const title = block.live ? 'Thinking' : thoughtFor(block.seconds);
  const hasText = block.text.trim().length > 0;
  if (!hasText && !block.live) {
    return <p data-slot="chat-thinking" className="text-[13px] text-muted-foreground">{title}</p>;
  }
  return (
    <div data-slot="chat-thinking" data-live={block.live ? 'true' : 'false'} className="min-w-0">
      <button
        type="button"
        aria-expanded={open}
        aria-controls={bodyId}
        onClick={() => setChosen(!open)}
        className="group inline-flex items-center gap-1.5 rounded-md py-0.5 text-[13px] text-muted-foreground hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
      >
        {block.live && <Spinner />}
        <span className={block.live ? 'animate-pulse motion-reduce:animate-none' : undefined}>{title}</span>
        <ChevronRight aria-hidden="true" className={cn('size-3.5 transition-transform motion-reduce:transition-none', open && 'rotate-90')} />
      </button>
      {open && (
        <div id={bodyId} className="mt-1.5 ml-[7px] max-h-80 overflow-y-auto border-l border-border pl-3.5 text-[13px] text-muted-foreground">
          <ChatMarkdown text={block.text} />
        </div>
      )}
    </div>
  );
}

// ── Steps ───────────────────────────────────────────────────

function liveAgents(step: ViewStep, pictureUrl: (id: string) => string): LiveAgent[] {
  return (step.agents ?? []).map((agent) => ({
    description: agent.description,
    steps: agent.steps.map((inner) => toAssistantStep(inner, pictureUrl)),
    done: !agent.running,
    ok: agent.ok === true,
    report: agent.report ?? '',
    stopped: agent.stopped ?? null,
  }));
}

/** A step still running: its line with a spinner, or "Running N agents" with each helper so far. */
function RunningStep({ step, pictureUrl }: { step: ViewStep; pictureUrl: (id: string) => string }) {
  if (step.tool === 'Agent') {
    return <LiveSteps entries={[{ kind: 'agents', label: step.label, agents: liveAgents(step, pictureUrl) }]} stage={null} />;
  }
  return <LiveSteps entries={[]} stage={step.label} />;
}

/** One step on its own line: running (spinner), or finished (folded to its command and output). */
function OneStep({ block, pictureUrl }: { block: Extract<ViewBlock, { type: 'step' }>; pictureUrl: (id: string) => string }) {
  return block.live ? <RunningStep step={block.step} pictureUrl={pictureUrl} /> : <StepItem step={toAssistantStep(block.step, pictureUrl)} />;
}

// ── Files ───────────────────────────────────────────────────

function iconFor(file: ChatFile) {
  const kind = fileKind(file);
  if (kind === 'PDF' || kind === 'Text' || kind === 'Markdown') return FileText;
  if (kind === 'CSV' || kind === 'Spreadsheet') return FileSpreadsheet;
  if (kind === 'Image') return FileImage;
  return FileIcon;
}

export function FileCards({ files, chatId }: { files: readonly ChatFile[]; chatId: string }) {
  if (!files.length) return null;
  return (
    <ul data-slot="chat-files" aria-label="Files it made" className="grid grid-cols-[repeat(auto-fill,minmax(min(100%,17rem),1fr))] gap-2">
      {files.map((file) => {
        const Icon = iconFor(file);
        const href = file.url || chatPaths.file(chatId, file.id);
        const meta = [fileKind(file), fileSize(file.size)].filter(Boolean).join(' · ');
        return (
          <li key={file.id} data-slot="chat-file" className="flex min-w-0 items-center gap-3 rounded-xl border bg-card px-3 py-2.5">
            <span className="grid size-9 shrink-0 place-items-center rounded-lg bg-muted text-muted-foreground" aria-hidden="true">
              <Icon className="size-[18px]" />
            </span>
            <span className="min-w-0 flex-1">
              <span className="block truncate text-sm font-medium" title={file.name}>{file.name}</span>
              <span className="block text-xs text-muted-foreground">{meta}</span>
            </span>
            <a
              href={href}
              download={file.name}
              aria-label={`Download ${file.name}`}
              className="inline-flex h-8 shrink-0 items-center gap-1.5 rounded-md border bg-background px-2.5 text-sm font-medium hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
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

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const timer = window.setTimeout(() => setCopied(false), 2000);
    return () => window.clearTimeout(timer);
  }, [copied]);
  return (
    <button
      type="button"
      onClick={() => void navigator.clipboard?.writeText(text).then(() => setCopied(true), () => setCopied(false))}
      title={copied ? 'Copied' : 'Copy answer'}
      aria-label={copied ? 'Copied' : 'Copy answer'}
      className="inline-grid size-[28px] shrink-0 place-items-center rounded-lg text-muted-foreground hover:bg-accent hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
    >
      {copied ? <Check className="size-3.5" aria-hidden="true" /> : <Copy className="size-3.5" aria-hidden="true" />}
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
}

export function AssistantMessage({ message, chatId, models, messageCapUsd, onReload }: AssistantMessageProps) {
  const running = message.status === 'running';
  const pictureUrl = (pictureId: string) => chatPaths.picture(chatId, pictureId);
  const { items, planAfter } = answerItems(message.blocks, running, Boolean(message.tasks?.length));
  const streaming = textStreaming(message);
  const lastText = items.reduce((last, item, at) => (item.kind === 'text' ? at : last), -1);
  const stopped = stoppedWords(message, messageCapUsd);
  const answerText = message.text || message.blocks.filter((block) => block.type === 'text').map((block) => block.text).join('\n\n');
  const plan = message.tasks?.length ? <PlanList tasks={toPlanTasks(message.tasks)} /> : null;

  return (
    <article data-slot="chat-assistant-message" data-status={message.status} aria-label="Answer" className="flex min-w-0 flex-col gap-3">
      {items.map((item, at) => (
        <div key={item.kind === 'steps' ? `steps-${item.indexes[0]}` : `${item.kind}-${item.index}`} className="min-w-0">
          {item.kind === 'thinking' && <ThinkingBlock block={message.blocks[item.index] as Extract<ViewBlock, { type: 'thinking' }>} />}
          {item.kind === 'text' && (
            <ChatMarkdown
              text={(message.blocks[item.index] as Extract<ViewBlock, { type: 'text' }>).text}
              streaming={streaming && at === lastText && item.index === message.blocks.length - 1}
            />
          )}
          {item.kind === 'step' && <OneStep block={message.blocks[item.index] as Extract<ViewBlock, { type: 'step' }>} pictureUrl={pictureUrl} />}
          {item.kind === 'steps' && (
            <AssistantSteps steps={item.indexes.map((index) => toAssistantStep((message.blocks[index] as Extract<ViewBlock, { type: 'step' }>).step, pictureUrl))} />
          )}
          {at === planAfter && plan && <div className="mt-2">{plan}</div>}
        </div>
      ))}
      {planAfter === null && plan}

      {running && !somethingLive(message) && <LiveSteps entries={[]} stage={message.id ? 'Working' : 'Sending your message'} />}

      <FileCards files={message.files} chatId={chatId} />

      {stopped && (
        <p data-slot="chat-stopped" data-stopped={message.stopped ?? undefined} className="flex items-start gap-2 text-sm text-muted-foreground">
          <CircleAlert className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
          <span>{stopped}</span>
        </p>
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

      {!running && (
        <div data-slot="chat-footer" className="flex min-w-0 items-center gap-1 text-xs text-muted-foreground">
          {answerText.trim() && <CopyButton text={answerText} />}
          <span className="min-w-0">{footerWords(message, models)}</span>
        </div>
      )}
    </article>
  );
}
