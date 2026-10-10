import { useEffect, useId, useState } from 'react';
import { Check, ChevronRight, Copy, File, FileText, ListChecks, Ruler } from 'lucide-react';

import type { CountertopResult } from '@/api/client';
import type { AssistantAnswer } from '@/api/assistantTypes';
import type { Finding, Outcome } from '@/data/types';
import { cn } from '@/lib/utils';
import { formatDelta } from '@/lib/countertop-results';
import { inchesOf } from '@/lib/countertop-strip';
import {
  answerParts,
  citeOutcome,
  matchesRecords,
  plainAnswer,
  queueKeyFor,
  resolveEvidence,
  rowOutcome,
  usableActions,
  actionKey,
  offeredByEvidence,
  BLOCKERS_SHOWN,
  type AnswerPart,
  type AssistantRecords,
  type Blocker,
  type CiteTarget,
} from '@/lib/assistant';
import { OutcomeBadge } from '@/components/ui/outcome-badge';
import { OutcomeIcon } from '@/components/ui/OutcomeIcon';
import { Button } from '@/components/ui/button';

/** Where the panel can take the reviewer. Navigation only: nothing here records a decision. */
export interface AssistantNavigation {
  openPage: (page: number) => void;
  showRow: (row: CountertopResult) => void;
  showFinding: (finding: Finding) => void;
  openQueue: (key: string) => void;
}

function openCite(target: CiteTarget, nav: AssistantNavigation) {
  if (target.kind === 'row') nav.showRow(target.row);
  else if (target.kind === 'finding') nav.showFinding(target.finding);
  else nav.openPage(target.page);
}

// ── Revealing the text ──────────────────────────────────────

type Token = { kind: 'text'; text: string } | { kind: 'cite'; part: Extract<AnswerPart, { kind: 'cite' }> };

function tokensOf(parts: readonly AnswerPart[]): Token[] {
  return parts.flatMap((part): Token[] =>
    part.kind === 'cite' ? [{ kind: 'cite', part }] : part.text.split(/(\s+)/).filter(Boolean).map((text) => ({ kind: 'text', text })),
  );
}

/** The first `count` tokens, with neighbouring words joined back into one run of text. */
function visibleParts(tokens: readonly Token[], count: number): AnswerPart[] {
  const parts: AnswerPart[] = [];
  for (const token of tokens.slice(0, count)) {
    const last = parts[parts.length - 1];
    if (token.kind === 'cite') parts.push(token.part);
    else if (last?.kind === 'text') parts[parts.length - 1] = { kind: 'text', text: last.text + token.text };
    else parts.push({ kind: 'text', text: token.text });
  }
  return parts;
}

function reducedMotion(): boolean {
  return typeof window === 'undefined' || typeof window.matchMedia !== 'function' || window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

/**
 * How many tokens are on screen. The answer arrives whole (the server's guard accepts or refuses it
 * as one); it is shown a few words at a time, in under two seconds, unless motion is reduced.
 */
function useReveal(total: number, enabled: boolean): number {
  const animate = enabled && !reducedMotion();
  const [shown, setShown] = useState(animate ? 0 : total);
  useEffect(() => {
    if (!animate) return;
    const step = Math.max(1, Math.ceil(total / 60));
    const timer = window.setInterval(() => setShown((n) => Math.min(total, n + step)), 28);
    return () => window.clearInterval(timer);
  }, [animate, total]);
  return animate ? shown : total;
}

// ── Pieces ──────────────────────────────────────────────────

/** The outcome glyph's colour, always beside its shape (and its word in the chip's name). */
const GLYPH_TONE: Record<Outcome, string> = {
  PASS: 'text-outcome-pass-fg',
  FAIL: 'text-outcome-fail-fg',
  REVIEW_REQUIRED: 'text-outcome-review-fg',
  NOT_FOUND: 'text-outcome-missing-fg',
  NO_APPLICABLE_RULE: 'text-muted-foreground',
};

function CiteChip({ part, nav }: { part: Extract<AnswerPart, { kind: 'cite' }>; nav: AssistantNavigation }) {
  const page = part.target.page;
  const recorded = citeOutcome(part.target);
  const name = `Open ${page !== null ? `page ${page}` : part.label} on the drawing${recorded ? `: ${recorded.word}` : ''}`;
  return (
    <button
      type="button"
      data-slot="assistant-cite"
      data-outcome={recorded?.outcome}
      onClick={() => openCite(part.target, nav)}
      title={name}
      aria-label={name}
      // Numbers in the number face; a chip that names no page shows its words in the word face.
      className={cn(
        'mx-0.5 inline-flex h-7 items-center gap-1 rounded-md bg-muted px-1.5 align-middle text-xs text-foreground hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none',
        page !== null ? 'num' : 'font-sans',
      )}
    >
      {recorded ? (
        <OutcomeIcon outcome={recorded.outcome} size={12} className={GLYPH_TONE[recorded.outcome]} />
      ) : (
        <File className="size-3 text-muted-foreground" aria-hidden="true" />
      )}
      {part.label}
    </button>
  );
}

function ListRow({ onClick, label, children }: { onClick: () => void; label: string; children: React.ReactNode }) {
  return (
    <li className="border-t first:border-t-0">
      <button
        type="button"
        onClick={onClick}
        aria-label={label}
        className="flex min-h-[52px] w-full items-center gap-3 px-3.5 py-2.5 text-left hover:bg-muted focus-visible:bg-muted focus-visible:outline-none"
      >
        {children}
        <ChevronRight className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
      </button>
    </li>
  );
}

function BlockerList({ items, nav }: { items: readonly Blocker[]; nav: AssistantNavigation }) {
  const shown = items.slice(0, BLOCKERS_SHOWN);
  const rest = items.slice(BLOCKERS_SHOWN);
  const more = rest.reduce((sum, item) => sum + item.count, 0);
  return (
    <div className="flex flex-col gap-1.5">
      <ul data-slot="assistant-blockers" aria-label="What is left before sign-off" className="overflow-hidden rounded-xl border bg-card">
        {shown.map((item) => (
          <ListRow
            key={item.key}
            onClick={() => nav.openQueue(item.key)}
            label={`${item.title}: ${item.word}. ${item.detail}. Open it in the queue`}
          >
            {/* Title and reason first so they line up; the result's badge sits at the end, where its width varies. */}
            <span className="min-w-0 flex-1">
              <span className="block truncate">
                {item.page !== null ? <>Page <span className="num">{item.page}</span></> : item.title}
              </span>
              <span className="block line-clamp-2 text-xs text-muted-foreground" title={item.detail}>
                {item.detail}
              </span>
            </span>
            <OutcomeBadge outcome={item.outcome} label={item.word} />
          </ListRow>
        ))}
      </ul>
      {more > 0 && (
        <p className="flex flex-wrap items-center gap-x-2 text-xs text-muted-foreground">
          <span>
            and <span className="num">{more}</span> more in the queue
          </span>
          <button
            type="button"
            onClick={() => nav.openQueue(rest[0].key)}
            className="min-h-7 rounded-md px-1.5 font-medium text-foreground underline underline-offset-2 hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
          >
            Open queue
          </button>
        </p>
      )}
    </div>
  );
}

/** Longer than this, a listed reason (often the readers' own words) is cut to two lines with "Show more". */
const LONG_REASON = 110;

function ListedPage({ item, nav }: { item: { page_number: number; reason: string }; nav: AssistantNavigation }) {
  const [expanded, setExpanded] = useState(false);
  const reasonId = useId();
  const long = item.reason.length > LONG_REASON;
  return (
    <li className="grid gap-0.5 border-t px-3.5 pt-1 pb-2.5 first:border-t-0">
      <button
        type="button"
        onClick={() => nav.openPage(item.page_number)}
        aria-label={`Page ${item.page_number}: open it on the drawing`}
        className="-mx-1.5 flex min-h-11 items-center gap-3 rounded-md px-1.5 text-left hover:bg-muted focus-visible:bg-muted focus-visible:outline-none"
      >
        <FileText className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
        <span className="min-w-0 flex-1">
          Page <span className="num">{item.page_number}</span>
        </span>
        <ChevronRight className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
      </button>
      <p id={reasonId} className={cn('pl-7 text-xs text-muted-foreground', long && !expanded && 'line-clamp-2')}>
        {item.reason}
      </p>
      {long && (
        <button
          type="button"
          aria-expanded={expanded}
          aria-controls={reasonId}
          onClick={() => setExpanded(!expanded)}
          className="ml-7 min-h-7 justify-self-start rounded-md px-1 text-xs font-medium text-foreground underline underline-offset-2 hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
        >
          {expanded ? 'Show less' : 'Show more'}
        </button>
      )}
    </li>
  );
}

function PageList({ items, title, nav }: { items: readonly { page_number: number; reason: string }[]; title: string; nav: AssistantNavigation }) {
  return (
    <div className="flex flex-col gap-1.5">
      <p className="text-xs text-muted-foreground">{title}</p>
      <ul aria-label={title} className="overflow-hidden rounded-xl border bg-card">
        {items.map((item, index) => (
          <ListedPage key={`${item.page_number}:${index}`} item={item} nav={nav} />
        ))}
      </ul>
    </div>
  );
}

/** One dimension line, drawn like a shop drawing's: ticks at both ends, the value over its middle. */
function DimensionLine({ name, value, width, dashed }: { name: string; value: string; width: number; dashed?: boolean }) {
  const tick = cn('absolute -top-[7px] h-[13px] border-l', dashed ? 'border-muted-foreground' : 'border-foreground');
  return (
    <div className="grid grid-cols-[58px_minmax(0,1fr)] items-center gap-2.5 text-xs text-muted-foreground">
      <span>{name}</span>
      <div className="relative h-[22px]" role="img" aria-label={`${name} ${value}`}>
        <span
          className={cn('absolute top-1/2 left-0 h-0 border-t', dashed ? 'border-dashed border-muted-foreground' : 'border-foreground')}
          style={{ width: `${width}%` }}
        >
          <span className={cn(tick, 'left-0')} />
          <span className={cn(tick, 'right-0')} />
        </span>
        <span
          className="num absolute top-1/2 -translate-x-1/2 -translate-y-1/2 bg-card px-1.5 text-xs text-foreground"
          style={{ left: `${Math.max(width / 2, 18)}%` }}
          aria-hidden="true"
        >
          {value}
        </span>
      </div>
    </div>
  );
}

/**
 * A countertop as the records hold it. Every number is the API row's exact text; the floats only
 * set the two lines' lengths. With either value missing or not exact, the values are written out
 * and no lines are drawn.
 */
export function CountertopEvidence({
  row,
  finding,
  queueKey,
  nav,
}: {
  row: CountertopResult;
  finding: Finding | null;
  /** The queue item to open, only when the queue lists it (`queueKeyFor`); null shows no queue button. */
  queueKey: string | null;
  nav: AssistantNavigation;
}) {
  const look = rowOutcome(row);
  const printed = row.printed_overall;
  const needed = row.expected_total;
  const pv = inchesOf(printed);
  const nv = inchesOf(needed);
  const drawn = pv !== null && nv !== null && pv > 0 && nv > 0;
  const longest = drawn ? Math.max(pv, nv) : 1;
  const delta = formatDelta(row.delta);
  const tolerance = finding?.tolerance ?? null;
  const notes = [row.hold?.reason, row.drawn_length_note, row.wall_layout.label ? `Walls: ${row.wall_layout.label}` : null].filter((note): note is string => Boolean(note));

  return (
    <section data-slot="assistant-countertop" aria-label={`Countertop on page ${row.page_number}`} className="rounded-xl border bg-card">
      <div className="flex items-center gap-2 px-3.5 py-2.5">
        <span className="min-w-0 flex-1 font-medium">
          Countertop · page <span className="num">{row.page_number}</span>
        </span>
        <OutcomeBadge outcome={look.outcome} label={look.word} />
      </div>
      <div className="grid gap-2.5 px-3.5 pt-0.5 pb-3">
        {drawn ? (
          <>
            <DimensionLine name="Printed" value={printed!.display} width={(100 * pv) / longest} />
            <DimensionLine name="Needed" value={needed!.display} width={(100 * nv) / longest} dashed />
          </>
        ) : (
          <dl className="grid grid-cols-[58px_minmax(0,1fr)] gap-x-2.5 gap-y-1 text-xs text-muted-foreground">
            <dt>Printed</dt>
            <dd className={printed ? 'num text-foreground' : 'text-muted-foreground'}>{printed?.display ?? 'not read'}</dd>
            <dt>Needed</dt>
            <dd className={needed ? 'num text-foreground' : 'text-muted-foreground'}>{needed?.display ?? 'not worked out'}</dd>
          </dl>
        )}
      </div>
      <div className="flex flex-wrap items-baseline justify-between gap-2 border-t px-3.5 py-2 text-xs text-muted-foreground">
        <span>
          Difference
          {tolerance && (
            <>
              , allowed <span className="num">{tolerance}</span>
            </>
          )}
        </span>
        {/* Neutral: the header's badge says the result with its glyph and word; colour alone never does. */}
        <span className="num text-sm text-foreground">{delta.text}</span>
      </div>
      {notes.length > 0 && (
        <ul className="grid gap-1 px-3.5 pb-2.5 text-xs text-muted-foreground">
          {notes.map((note) => (
            <li key={note}>{note}</li>
          ))}
        </ul>
      )}
      <div className="flex border-t">
        <button
          type="button"
          onClick={() => nav.showRow(row)}
          className="inline-flex min-h-11 flex-1 items-center justify-center gap-1.5 rounded-bl-xl px-2 text-sm hover:bg-accent focus-visible:bg-accent focus-visible:outline-none last:rounded-br-xl"
        >
          <Ruler className="size-4 text-muted-foreground" aria-hidden="true" />
          Show on drawing
        </button>
        {queueKey && (
          <button
            type="button"
            onClick={() => nav.openQueue(queueKey)}
            className="inline-flex min-h-11 flex-1 items-center justify-center gap-1.5 rounded-br-xl border-l px-2 text-sm hover:bg-accent focus-visible:bg-accent focus-visible:outline-none"
          >
            <ListChecks className="size-4 text-muted-foreground" aria-hidden="true" />
            Open in queue
          </button>
        )}
      </div>
    </section>
  );
}

// ── The answer ──────────────────────────────────────────────

export function AnswerView({
  answer,
  records,
  nav,
  reveal,
  latest,
  busy,
  onAsk,
  onRevealed,
}: {
  answer: AssistantAnswer;
  records: AssistantRecords;
  nav: AssistantNavigation;
  /** Show the words a few at a time (a fresh answer); false shows it whole. */
  reveal: boolean;
  /** Follow-up suggestions are offered after the latest answer only. */
  latest: boolean;
  busy: boolean;
  onAsk: (question: string) => void;
  /** Called once the whole answer is on screen (at once when nothing was revealed gradually). */
  onRevealed?: () => void;
}) {
  const parts = answerParts(answer.text, answer.citations, records);
  const tokens = tokensOf(parts);
  const shown = useReveal(tokens.length, reveal);
  const revealing = shown < tokens.length;
  const evidence = resolveEvidence(answer.evidence, records);
  // An action going where a card or list below already goes is the same button twice: left out.
  const offered = offeredByEvidence(evidence, records);
  const actions = usableActions(answer.actions, records).filter((action) => !offered.has(actionKey(action)));
  const [sourcesOpen, setSourcesOpen] = useState(false);
  const [copied, setCopied] = useState(false);
  const checked = matchesRecords(answer);

  // Tell the panel when the words are all there, so "Answer ready" is announced then and not before.
  useEffect(() => {
    if (reveal && !revealing) onRevealed?.();
  }, [reveal, revealing, onRevealed]);

  // "Copied" goes back to "Copy answer" after a moment, so the button can be used again.
  useEffect(() => {
    if (!copied) return;
    const timer = window.setTimeout(() => setCopied(false), 2000);
    return () => window.clearTimeout(timer);
  }, [copied]);

  function copy() {
    void navigator.clipboard?.writeText(plainAnswer(answer)).then(
      () => setCopied(true),
      () => setCopied(false),
    );
  }

  return (
    // One column that may shrink: an auto column would grow to the longest one-line (truncated) reason
    // and push the lists past the panel's edge.
    <div data-slot="assistant-answer" data-mode={answer.mode} className="grid min-w-0 grid-cols-[minmax(0,1fr)] gap-3">
      <p className="leading-relaxed break-words whitespace-pre-line">
        {visibleParts(tokens, shown).map((part, index) =>
          part.kind === 'cite' ? <CiteChip key={index} part={part} nav={nav} /> : part.text,
        )}
        {revealing && <span aria-hidden="true" className="ml-0.5 inline-block h-[15px] w-[7px] animate-pulse bg-foreground align-[-2px]" />}
      </p>

      {!revealing && (
        <>
          {evidence.map((item) =>
            item.kind === 'countertop' ? (
              <CountertopEvidence key={item.key} row={item.row} finding={item.finding} queueKey={queueKeyFor(item.row.row_id, records)} nav={nav} />
            ) : item.kind === 'blockers' ? (
              <BlockerList key={item.key} items={item.items} nav={nav} />
            ) : item.kind === 'no_countertop_pages' ? (
              <PageList key={item.key} items={item.items} title="Pages with no countertop: listed, nothing to decide" nav={nav} />
            ) : (
              <PageList key={item.key} items={item.items} title="Rows not checked: listed, nothing to decide" nav={nav} />
            ),
          )}

          {actions.length > 0 && (
            <div className="flex flex-wrap gap-2">
              {actions.map((action, index) => (
                <Button
                  key={`${action.kind}:${index}`}
                  type="button"
                  variant="outline"
                  size="sm"
                  className="font-sans"
                  onClick={() => (action.kind === 'open_page' ? nav.openPage(action.page_number) : nav.openQueue(action.queueKey))}
                >
                  {action.kind === 'open_page' ? <Ruler aria-hidden="true" /> : <ListChecks aria-hidden="true" />}
                  {action.label}
                </Button>
              ))}
            </div>
          )}

          <div className="-mt-1 flex flex-wrap items-center gap-0.5">
            <button
              type="button"
              onClick={copy}
              title={copied ? 'Copied' : 'Copy answer'}
              aria-label={copied ? 'Copied' : 'Copy answer'}
              className="inline-grid size-[30px] place-items-center rounded-lg text-muted-foreground hover:bg-accent hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
            >
              {copied ? <Check className="size-4" aria-hidden="true" /> : <Copy className="size-4" aria-hidden="true" />}
            </button>
            {answer.sources.length > 0 && (
              <button
                type="button"
                aria-expanded={sourcesOpen}
                onClick={() => setSourcesOpen(!sourcesOpen)}
                className="inline-flex h-[30px] items-center gap-1.5 rounded-lg px-2 text-xs text-muted-foreground hover:bg-accent hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
              >
                <FileText className="size-4" aria-hidden="true" />
                Sources
              </button>
            )}
            {checked && (
              <span
                data-slot="assistant-checked"
                title="Every number and result in this answer was matched against this review's records"
                className="ml-1.5 inline-flex items-center gap-1 text-xs text-muted-foreground"
              >
                <Check className="size-3.5" aria-hidden="true" />
                Matches the records
              </span>
            )}
          </div>
          {sourcesOpen && answer.sources.length > 0 && (
            <ul aria-label="Sources" className="grid gap-1 rounded-lg bg-muted px-3 py-2.5 text-xs text-muted-foreground">
              {answer.sources.map((source, index) => (
                <li key={index}>{source}</li>
              ))}
            </ul>
          )}

          {latest && answer.suggestions.length > 0 && (
            <div role="group" className="flex flex-wrap gap-2" aria-label="Ask next">
              {answer.suggestions.map((suggestion) => (
                <button
                  key={suggestion}
                  type="button"
                  disabled={busy}
                  onClick={() => onAsk(suggestion)}
                  className="min-h-[34px] rounded-full border bg-background px-3 py-1.5 text-left text-sm hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none disabled:opacity-50"
                >
                  {suggestion}
                </button>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}
