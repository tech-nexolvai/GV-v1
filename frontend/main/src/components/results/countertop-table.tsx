import { Fragment, useRef, useState } from 'react';
import { getCoreRowModel, getSortedRowModel, useReactTable, type ColumnDef, type SortingState } from '@tanstack/react-table';
import { ChevronRight, CircleDashed, FileSearch, LayoutPanelTop, Lock, PencilLine } from 'lucide-react';

import { cn } from '@/lib/utils';
import type { CountertopResult } from '@/api/client';
import type { Finding } from '@/data/types';
import { decisionWords, formatDelta, initials, isSplitPage, rowNeedsYou, sortValue } from '@/lib/countertop-results';
import { architectState, awaitsPairing, headlinePair, pairLabel, pairedByWords } from '@/lib/architect';
import { SortableHeader } from '@/components/data-table/data-table';
import { OutcomeBadge } from '@/components/ui/outcome-badge';
import { OutcomeIcon } from '@/components/ui/OutcomeIcon';
import { OUTCOME_LABELS } from '@/data/outcomeLabels';
import { Button } from '@/components/ui/button';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip';
import { WallGlyph } from './wall-glyph';
import { CountertopStrip } from './CountertopStrip';
import { ArchitectDelta, ArchitectLine, ArchitectPairs, ArchitectStatus } from './architect-line';
import { SplitPageNote } from './split-page-note';

export interface RowActions {
  onShowDrawing: (row: CountertopResult) => void;
  onOpenCard: (row: CountertopResult) => void;
  /** Present only when the row's finding can take a decision here. */
  onDecide: (row: CountertopResult) => void;
  canDecide: (row: CountertopResult) => boolean;
  /** The row's architect finding as the page holds it (its decision, if any). */
  architectFinding?: (row: CountertopResult) => Finding | undefined;
  /** A recorded architect result that needs a decision: the usual Decide dialog. */
  onDecideArchitect?: (row: CountertopResult) => void;
  /** A pairing to confirm or make: the queue, at this row's "Matches the architect?" item. */
  onPairArchitect?: (row: CountertopResult) => void;
}

/**
 * One row per countertop (#1039): what was printed, what was needed, the difference, the walls and
 * who decided — every number exactly as the API sent it. Rows arrive filtered and sorted.
 *
 * Keyboard: ↑/↓ move between rows, Enter opens a row's details, D opens Decide when it applies.
 */
const COLUMNS: ColumnDef<CountertopResult>[] = [
  { id: 'page', accessorFn: (r) => r.page_number },
  { id: 'label', accessorFn: (r) => r.label },
  { id: 'printed', accessorFn: (r) => sortValue(r.printed_overall) },
  { id: 'needed', accessorFn: (r) => sortValue(r.expected_total) },
  { id: 'difference', accessorFn: (r) => sortValue(r.delta) },
];

export function CountertopTable({ rows, actions }: { rows: CountertopResult[]; actions: RowActions }) {
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const body = useRef<HTMLTableSectionElement>(null);
  // Column sorting is the reviewer's choice; until they pick one, rows keep the order they came in
  // (needs you → FAIL → PASS → not checkable, then page).
  const [sorting, setSorting] = useState<SortingState>([]);
  // eslint-disable-next-line react-hooks/incompatible-library
  const table = useReactTable({
    data: rows,
    columns: COLUMNS,
    state: { sorting },
    onSortingChange: setSorting,
    getRowId: (row) => row.row_id,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
  });
  const ordered = table.getRowModel().rows.map((r) => r.original);
  const header = (id: string, label: string, right = false) => {
    const column = table.getColumn(id);
    return column ? <SortableHeader compact column={column} className={right ? 'ml-auto' : undefined}>{label}</SortableHeader> : label;
  };

  function toggle(id: string) {
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function onRowKey(event: React.KeyboardEvent<HTMLTableRowElement>, row: CountertopResult) {
    if (event.target !== event.currentTarget) return; // keys inside buttons stay theirs
    const rowsEls = [...(body.current?.querySelectorAll<HTMLTableRowElement>('tr[data-row-id]') ?? [])];
    const index = rowsEls.indexOf(event.currentTarget);
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      rowsEls[index + (event.key === 'ArrowDown' ? 1 : -1)]?.focus();
    } else if (event.key === 'Enter') {
      event.preventDefault();
      toggle(row.row_id);
    } else if ((event.key === 'd' || event.key === 'D') && actions.canDecide(row)) {
      event.preventDefault();
      actions.onDecide(row);
    }
  }

  return (
    <TooltipProvider delayDuration={250}>
    <div data-slot="countertop-table">
      {/* Wide screens: a real table. */}
      <div className="hidden overflow-x-auto rounded-xl border bg-card md:block">
        <Table>
          <TableHeader className="bg-muted/40">
            <TableRow>
              <TableHead className="w-8"><span className="sr-only">Details</span></TableHead>
              <TableHead className="w-16">{header('page', 'Page')}</TableHead>
              <TableHead>{header('label', 'Countertop')}</TableHead>
              <TableHead>Result</TableHead>
              <TableHead className="text-right">{header('printed', 'Printed', true)}</TableHead>
              <TableHead className="text-right">{header('needed', 'Needed', true)}</TableHead>
              <TableHead className="text-right">{header('difference', 'Difference', true)}</TableHead>
              <TableHead>Walls</TableHead>
              <TableHead>Decided by</TableHead>
              <TableHead className="text-right"><span className="sr-only">Actions</span></TableHead>
            </TableRow>
          </TableHeader>
          <TableBody ref={body}>
            {ordered.map((row) => {
              const open = expanded.has(row.row_id);
              return (
                <Fragment key={row.row_id}>
                  <TableRow
                    data-row-id={row.row_id}
                    tabIndex={0}
                    aria-expanded={open}
                    onKeyDown={(e) => onRowKey(e, row)}
                    className={cn('outline-none focus-visible:bg-accent/70', rowNeedsYou(row) && 'bg-outcome-review-bg/40', architectState(row.architect ?? null) !== 'none' && 'border-b-0')}
                  >
                    <TableCell className="pr-0">
                      <Button variant="ghost" size="icon-xs" aria-label={open ? 'Hide details' : 'Show details'} aria-expanded={open} onClick={() => toggle(row.row_id)}>
                        <ChevronRight className={cn('transition-transform motion-reduce:transition-none', open && 'rotate-90')} />
                      </Button>
                    </TableCell>
                    <TableCell className="num">{row.page_number}</TableCell>
                    <TableCell className="max-w-40">
                      <div className="flex flex-col gap-1">
                        <span className="truncate font-medium" title={row.label}>{row.label}</span>
                        {row.hold && <HoldChip hold={row.hold} />}
                        {/* A split page's reason is the whole story (#1093): in the row, not only on hover. */}
                        {row.hold && isSplitPage(row) && (
                          <span data-slot="split-reason" className="line-clamp-3 text-xs whitespace-normal text-muted-foreground" title={row.hold.reason}>{row.hold.reason}</span>
                        )}
                      </div>
                    </TableCell>
                    <TableCell><ResultCell row={row} /></TableCell>
                    <TableCell className="num text-right">{row.printed_overall?.display ?? '—'}</TableCell>
                    <TableCell className="num text-right">{row.expected_total?.display ?? '—'}</TableCell>
                    <TableCell className="text-right"><Delta row={row} /></TableCell>
                    <TableCell>{isSplitPage(row) ? <SplitWalls /> : <WallGlyph layout={row.wall_layout} compactSource />}</TableCell>
                    <TableCell><DecidedBy row={row} /></TableCell>
                    <TableCell className="text-right"><Actions row={row} actions={actions} /></TableCell>
                  </TableRow>
                  <ArchitectRow row={row} actions={actions} />
                  {open && (
                    <TableRow className="hover:bg-transparent">
                      <TableCell colSpan={10} className="bg-muted/30 whitespace-normal">
                        <RowDetails row={row} />
                      </TableCell>
                    </TableRow>
                  )}
                </Fragment>
              );
            })}
          </TableBody>
        </Table>
      </div>

      {/* Phones: the same rows, stacked. */}
      <ul className="flex flex-col gap-2 md:hidden" aria-label="Countertops">
        {ordered.map((row) => {
          const open = expanded.has(row.row_id);
          return (
            <li key={row.row_id} className={cn('rounded-xl border bg-card p-3', rowNeedsYou(row) && 'border-outcome-review-fg/40')}>
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <p className="truncate font-medium">{row.label}</p>
                  <p className="text-xs text-muted-foreground">Page <span className="num">{row.page_number}</span></p>
                </div>
                <ResultCell row={row} />
              </div>
              <dl className="mt-2 grid grid-cols-3 gap-2 text-xs">
                <div><dt className="text-muted-foreground">Printed</dt><dd className="num text-sm">{row.printed_overall?.display ?? '—'}</dd></div>
                <div><dt className="text-muted-foreground">Needed</dt><dd className="num text-sm">{row.expected_total?.display ?? '—'}</dd></div>
                <div><dt className="text-muted-foreground">Difference</dt><dd className="text-sm"><Delta row={row} /></dd></div>
              </dl>
              <div className="mt-2 flex flex-wrap items-center justify-between gap-2">
                <div className="flex items-center gap-2">
                  {isSplitPage(row) ? <SplitWalls /> : <WallGlyph layout={row.wall_layout} />}
                  <DecidedBy row={row} />
                </div>
                <Actions row={row} actions={actions} />
              </div>
              {row.hold && isSplitPage(row) ? (
                <SplitPageNote hold={row.hold} className="mt-2" />
              ) : row.hold ? (
                <div className="mt-2"><HoldChip hold={row.hold} /></div>
              ) : null}
              <ArchitectLine result={row.architect} className="mt-2 border-t pt-2" clamp />
              <ArchitectAction row={row} actions={actions} />
              {/* A split page has no pieces to show: its note above says why. */}
              {!isSplitPage(row) && (
                <>
                  <button type="button" className="mt-2 text-xs text-muted-foreground underline-offset-2 hover:underline" aria-expanded={open} onClick={() => toggle(row.row_id)}>
                    {open ? 'Hide pieces' : 'Show pieces'}
                  </button>
                  {open && <div className="mt-2"><RowDetails row={row} /></div>}
                </>
              )}
            </li>
          );
        })}
      </ul>
    </div>
    </TooltipProvider>
  );
}

function ResultCell({ row }: { row: CountertopResult }) {
  return (
    <span className="inline-flex flex-col items-start gap-1">
      {row.outcome && !row.needs_decision && (row.outcome === 'REVIEW_REQUIRED' || row.outcome === 'NOT_FOUND') ? (
        // Decided by a reviewer: the check itself could not decide. Neutral, so a finished review
        // does not look unfinished; the reviewer's choice is in "Decided by".
        <span title={`Recorded result: ${OUTCOME_LABELS[row.outcome]}`} className="inline-flex items-center gap-1 rounded-full border border-dashed px-2 py-0.5 font-sans text-xs text-muted-foreground">
          <CircleDashed className="size-3.5" aria-hidden="true" /> Not checkable
        </span>
      ) : row.outcome ? (
        <OutcomeBadge outcome={row.outcome} />
      ) : (
        <span className="inline-flex items-center gap-1 rounded-full border border-dashed px-2 py-0.5 font-sans text-xs text-muted-foreground">
          <CircleDashed className="size-3.5" aria-hidden="true" /> Not checked
        </span>
      )}
      {row.needs_decision && row.outcome !== 'REVIEW_REQUIRED' && (
        <span className="inline-flex items-center gap-1 font-sans text-xs font-medium text-outcome-review-fg">
          <OutcomeIcon outcome="REVIEW_REQUIRED" size={13} /> Needs you
        </span>
      )}
    </span>
  );
}

function Delta({ row }: { row: CountertopResult }) {
  const { text, sign } = formatDelta(row.delta);
  if (sign === null) return <span className="num text-muted-foreground">—</span>;
  const ok = sign === 0;
  return (
    <span className={cn('num inline-flex items-center gap-1 font-medium', ok ? 'text-outcome-pass-fg' : 'text-outcome-fail-fg')}>
      <OutcomeIcon outcome={ok ? 'PASS' : 'FAIL'} size={13} />
      {text}
    </span>
  );
}

function DecidedBy({ row }: { row: CountertopResult }) {
  // Still needed means not decided, whatever was recorded before: an evidence-only confirmation,
  // an expired exception or a correction awaiting a new run must never read as a finished decision.
  if (row.needs_decision) {
    if (row.reviewer_decision?.action === 'correct') {
      return <span className="text-xs text-outcome-review-fg">Corrected — run checks again</span>;
    }
    return <span className="text-xs text-muted-foreground">Pending</span>;
  }
  if (row.reviewer_decision) {
    const decision = row.reviewer_decision;
    return (
      <Tooltip>
        <TooltipTrigger asChild>
          <span tabIndex={0} className="inline-flex items-center gap-1.5 rounded-md text-xs outline-none focus-visible:ring-2 focus-visible:ring-ring">
            <span aria-hidden="true" className="num flex size-6 items-center justify-center rounded-full bg-secondary text-[10px] font-medium">{initials(decision.actor)}</span>
            <span>{decisionWords(decision.action, row.outcome)}</span>
          </span>
        </TooltipTrigger>
        <TooltipContent className="max-w-72">
          {decision.actor}{decision.note ? ` — “${decision.note}”` : ''}
        </TooltipContent>
      </Tooltip>
    );
  }
  if (!row.needs_decision && (row.outcome === 'PASS' || row.outcome === 'FAIL')) {
    return <span className="text-xs text-muted-foreground">Automatic</span>;
  }
  return <span className="text-xs text-muted-foreground">—</span>;
}

function Actions({ row, actions }: { row: CountertopResult; actions: RowActions }) {
  const decide = actions.canDecide(row);
  // A split page (#1093) opens on its page with nothing outlined; it has no countertop card, because
  // no line was chosen to read.
  const split = isSplitPage(row);
  return (
    <span className="inline-flex items-center justify-end gap-0.5">
      <Tooltip>
        <TooltipTrigger asChild>
          <Button variant="ghost" size="icon-xs" aria-label="Show on drawing" disabled={!row.row_location && !split} onClick={() => actions.onShowDrawing(row)}>
            <FileSearch />
          </Button>
        </TooltipTrigger>
        <TooltipContent>{row.row_location ? 'Show on drawing' : split ? 'Show the page (no line chosen)' : 'No stored drawing location'}</TooltipContent>
      </Tooltip>
      {!split && (
        <Tooltip>
          <TooltipTrigger asChild>
            <Button variant="ghost" size="icon-xs" aria-label="Open countertop card" onClick={() => actions.onOpenCard(row)}>
              <LayoutPanelTop />
            </Button>
          </TooltipTrigger>
          <TooltipContent>Open countertop card</TooltipContent>
        </Tooltip>
      )}
      {decide && (
        <Button size="sm" className="ml-1" onClick={() => actions.onDecide(row)}>Decide</Button>
      )}
    </span>
  );
}

function HoldChip({ hold }: { hold: NonNullable<CountertopResult['hold']> }) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span tabIndex={0} data-hold={hold.code} className="inline-flex w-fit max-w-full items-center gap-1 rounded-full border border-dashed px-2 py-0.5 font-sans text-xs text-muted-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring">
          <CircleDashed className="size-3" aria-hidden="true" />
          <span className="truncate">{isSplitPage({ hold }) ? 'No line chosen' : 'Held'}</span>
          {/* A split page's reason is on screen beside the chip; said once. */}
          {!isSplitPage({ hold }) && <span className="sr-only">: {hold.reason}</span>}
        </span>
      </TooltipTrigger>
      <TooltipContent className="max-w-80">{hold.reason}</TooltipContent>
    </Tooltip>
  );
}

/** No walls to ask about on a split page (#1093): no line was chosen, and it has no countertop card. */
function SplitWalls() {
  return <span data-slot="split-walls" className="text-muted-foreground" title="No line chosen, so no walls">—</span>;
}

const SOURCE_ICON = { sealed: Lock, typed: PencilLine, missing: CircleDashed } as const;
const SOURCE_WORD = { sealed: 'both AIs agreed', typed: 'typed by a reviewer', missing: 'missing' } as const;

function RowDetails({ row }: { row: CountertopResult }) {
  return (
    <div className="flex flex-col gap-3 py-1 font-sans">
      {/* A split page (#1093) has no line, so no pieces and no picture: its note says why. */}
      {row.hold && isSplitPage(row) ? <SplitPageNote hold={row.hold} className="max-w-2xl" /> : <ReadDetails row={row} />}
      {row.architect && row.architect.compared.length > 0 && (
        <div className="flex flex-col gap-1">
          <p className="text-xs font-medium">Matches the architect</p>
          <ArchitectPairs result={row.architect} />
          {row.architect.reason && <p className="max-w-2xl text-xs text-muted-foreground">{row.architect.reason}</p>}
        </div>
      )}
      {/* The whole "not compared" reason, which the table's one line cuts short. */}
      {row.architect && architectState(row.architect) === 'not-compared' && (
        <div className="flex flex-col gap-1">
          <p className="text-xs font-medium">Matches the architect</p>
          <p className="max-w-2xl"><ArchitectStatus result={row.architect} /></p>
        </div>
      )}
    </div>
  );
}

/** What was read on a countertop's line: its pieces, its hold, and its picture. */
function ReadDetails({ row }: { row: CountertopResult }) {
  return (
    <>
      <div className="flex flex-wrap items-center gap-1.5" aria-label="Pieces">
        {row.pieces.length === 0 && <span className="text-xs text-muted-foreground">No pieces read</span>}
        {row.pieces.map((piece) => {
          const Icon = SOURCE_ICON[piece.source];
          return (
            <span
              key={piece.index}
              title={`Piece ${piece.index + 1}${piece.kind ? ` (${piece.kind})` : ''}: ${SOURCE_WORD[piece.source]}`}
              className={cn(
                'inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 text-xs',
                piece.source === 'missing' ? 'border-dashed text-muted-foreground' : 'bg-background',
              )}
            >
              <Icon className="size-3 text-muted-foreground" aria-hidden="true" />
              <span className="num">{piece.value?.display ?? '—'}</span>
              <span className="sr-only">, {SOURCE_WORD[piece.source]}</span>
            </span>
          );
        })}
        {row.field_cut_per_end && row.field_cut_count !== null && (
          <span className="inline-flex items-center gap-1 rounded-md border border-dotted px-1.5 py-0.5 text-xs text-muted-foreground">
            field cut <span className="num">{row.field_cut_per_end.display} × {row.field_cut_count}</span>
          </span>
        )}
        {row.expected_total && (
          <span className="text-xs text-muted-foreground">
            = <span className="num text-foreground">{row.expected_total.display}</span> needed
          </span>
        )}
      </div>
      {row.hold && <p className="text-xs text-muted-foreground"><HoldChip hold={row.hold} /> <span className="ml-1">{row.hold.reason}</span></p>}
      <CountertopStrip row={row} />
    </>
  );
}

/**
 * The architect's line under the vendor's (#1085), in the same columns: what the architect's drawing
 * says under "Printed", the difference under "Difference", the result under "Result". A row the
 * architect's drawing has nothing comparable for gets one grey line with the reason.
 */
function ArchitectRow({ row, actions }: { row: CountertopResult; actions: RowActions }) {
  const result = row.architect ?? null;
  const state = architectState(result);
  if (!result || state === 'none') return null;
  const needsYou = result.needs_decision;
  if (state === 'not-compared') {
    return (
      <TableRow data-architect-row={row.row_id} className={cn('text-xs hover:bg-transparent', rowNeedsYou(row) && 'bg-outcome-review-bg/40')}>
        <TableCell colSpan={2} className="py-1.5" />
        {/* One line: the same reason often repeats on every row; the whole of it is in the title. */}
        <TableCell colSpan={8} className="max-w-0 py-1.5">
          <div className="truncate" title={result.not_compared_reason ?? undefined}>
            <span className="mr-2 font-medium text-muted-foreground">Architect</span>
            <ArchitectStatus result={result} />
          </div>
        </TableCell>
      </TableRow>
    );
  }
  const pair = headlinePair(result);
  const pairedBy = pairedByWords(result);
  const finding = actions.architectFinding?.(row);
  return (
    <TableRow data-architect-row={row.row_id} className={cn('text-xs hover:bg-transparent', rowNeedsYou(row) && 'bg-outcome-review-bg/40')}>
      <TableCell colSpan={2} className="py-1.5" />
      <TableCell className="max-w-40 py-1.5">
        <div className="flex flex-col">
          <span className="font-medium text-muted-foreground">Architect</span>
          {pairedBy && <span className="truncate text-muted-foreground" title={pairedBy}>{pairedBy}</span>}
          {result.compared.length > 1 && <span className="text-muted-foreground">{result.compared.length} compared: see details</span>}
        </div>
      </TableCell>
      <TableCell className="py-1.5 whitespace-normal"><ArchitectStatus result={result} /></TableCell>
      <TableCell className="py-1.5 text-right">
        <span className="sr-only">Architect&apos;s drawing says </span>
        {/* A piece's width is not the overall: said so, beside the number. */}
        {pair?.kind === 'piece' && <span className="text-muted-foreground">{pairLabel(pair)}: </span>}
        <span className="num">{pair?.architect_display ?? '—'}</span>
      </TableCell>
      <TableCell className="num py-1.5 text-right text-muted-foreground">—</TableCell>
      <TableCell className="py-1.5 text-right">{pair ? <ArchitectDelta pair={pair} neutral={awaitsPairing(result)} /> : '—'}</TableCell>
      <TableCell className="py-1.5" />
      <TableCell className="py-1.5">
        {needsYou ? (
          <span className="text-muted-foreground">Pending</span>
        ) : finding?.reviewer_action ? (
          <span>{decisionWords(finding.reviewer_action, result.outcome ?? null)}{finding.reviewed_by ? <span className="text-muted-foreground"> · {finding.reviewed_by}</span> : null}</span>
        ) : result.outcome === 'PASS' || result.outcome === 'FAIL' ? (
          <span className="text-muted-foreground">Automatic</span>
        ) : (
          <span className="text-muted-foreground">—</span>
        )}
      </TableCell>
      <TableCell className="py-1.5 text-right"><ArchitectAction row={row} actions={actions} /></TableCell>
    </TableRow>
  );
}

/** The one action an architect line offers, when it needs the reviewer. */
function ArchitectAction({ row, actions }: { row: CountertopResult; actions: RowActions }) {
  const result = row.architect ?? null;
  const state = architectState(result);
  if (!result?.needs_decision) return null;
  if ((state === 'confirm' || state === 'unpaired') && actions.onPairArchitect) {
    return (
      <Button size="sm" variant="outline" className="ml-1" onClick={() => actions.onPairArchitect?.(row)}>
        {state === 'confirm' ? 'Confirm the pairing…' : 'Pair it…'}
      </Button>
    );
  }
  if (state === 'compared' && actions.onDecideArchitect && actions.architectFinding?.(row)) {
    return (
      <Button size="sm" className="ml-1" onClick={() => actions.onDecideArchitect?.(row)}>Decide</Button>
    );
  }
  return null;
}
