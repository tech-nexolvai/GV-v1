import { useEffect, useEffectEvent, useRef, useState } from 'react';
import { CheckCircle2, ChevronLeft, ChevronRight, History, X } from 'lucide-react';

import { getArchitectPairing, listFindingActions, listRules, listSlotReaderRows, reviewSlotReaderRow, type CountertopResult } from '@/api/client';
import { useAsync } from '@/api/useAsync';
import type { Finding } from '@/data/types';
import { decisionWords, formatDelta, isSplitPage } from '@/lib/countertop-results';
import { targetFromArchitect, targetFromFinding, targetFromRow, type Mark } from '@/lib/drawing-viewer';
import { architectState, pairedByWords } from '@/lib/architect';
import {
  buildQueue,
  itemStatus,
  nextOpen,
  progressOf,
  findingIdOf,
  unaccountedBlocking,
  wallQuestion,
  wallWords,
  type ItemStatus,
  type LiveData,
  type QueueItem,
  type WallQuestion,
} from '@/lib/needs-you-queue';
import type { NextAction, NextActionKind } from '@/lib/review-stage';
import { cn } from '@/lib/utils';
import { CountertopStrip } from '@/components/results/CountertopStrip';
import { SplitPageNote } from '@/components/results/split-page-note';
import { CarriedOver } from '@/components/results/carried-over';
import { ArchitectLine, ArchitectPairs, ArchitectStatus } from '@/components/results/architect-line';
import { ArchitectPairingPanel } from './architect-pairing';
import { DecisionFields } from '@/components/results/decision-form';
import { useDecisionDraft, type DecideHandlers } from '@/components/results/use-decision-draft';
import { WallGlyph, WallLayoutPicture } from '@/components/results/wall-glyph';
import { DrawnLengthNote } from '@/components/results/drawn-length-note';
import { DrawingViewer, MarkNotes } from '@/components/drawing/drawing-viewer';
import { TonePill } from '@/components/drawing/page-canvas';
import { OutcomeIcon } from '@/components/ui/OutcomeIcon';
import { Button } from '@/components/ui/button';
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import { Progress } from '@/components/ui/progress';
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group';
import { TooltipProvider } from '@/components/ui/tooltip';

export interface QueueProps {
  open: boolean;
  /** Changes each time the queue is opened: the item list is taken then, and kept while it is open. */
  opening: number;
  onOpenChange: (open: boolean) => void;
  rows: readonly CountertopResult[];
  /** False while the countertop rows load: the list waits for them, as it waits for readiness. */
  rowsReady: boolean;
  findings: readonly Finding[];
  /** The readiness API's blocking set; null while it loads. */
  blocking: ReadonlySet<string> | null;
  projectId: string;
  packageId: string;
  handlers: DecideHandlers;
  /** The review's one next action (`reviewStage`), offered when nothing is left. */
  next: NextAction;
  onAct: (kind: NextActionKind) => void;
  /** A wall answer was saved: the checks must run again before it shows in a result. */
  onWallSaved: () => void;
  /** An architect pairing was saved (#1085): likewise, only a new check run uses it. */
  onPairingSaved?: () => void;
  onOpenCard: (row: CountertopResult) => void;
  /** Open at this item (its key), when it is in the list; otherwise at the first open one. */
  startAt?: string | null;
}

/**
 * The "Needs you" queue (#1050): every item still blocking sign-off, one at a time, with the
 * countertop picture, the drawing and the decision together. Decisions go through the page's own
 * handlers and the shared decision form; wall answers through the countertop card's endpoint. The
 * server says what is blocking; the queue only walks through it.
 */
export function NeedsYouQueue(props: QueueProps) {
  return (
    <Dialog open={props.open} onOpenChange={props.onOpenChange}>
      <DialogContent
        showCloseButton={false}
        // Focus the queue itself, not its first button: Enter must save, not press "Previous".
        onOpenAutoFocus={(event) => {
          event.preventDefault();
          (event.currentTarget as HTMLElement | null)?.querySelector<HTMLElement>('[data-slot="needs-you-queue"]')?.focus();
        }}
        className="top-0 left-0 flex h-dvh max-h-none w-screen max-w-none translate-x-0 translate-y-0 flex-col gap-0 rounded-none border-0 p-0 sm:max-w-none"
      >
        {props.open && <QueueBody key={props.opening} {...props} />}
      </DialogContent>
    </Dialog>
  );
}

const IN_FIELD = 'input, textarea, select, [contenteditable="true"]';

function firstOpen(items: readonly QueueItem[], live: LiveData, startAt?: string | null): number {
  const asked = startAt ? items.findIndex((item) => item.key === startAt) : -1;
  if (asked >= 0) return asked;
  return Math.max(0, items.findIndex((item) => itemStatus(item, live) === 'open'));
}

function QueueBody({ rows, rowsReady, findings, blocking, projectId, packageId, handlers, next, onAct, onOpenChange, onWallSaved, onPairingSaved, onOpenCard, startAt }: QueueProps) {
  const [wallsSaved, setWallsSaved] = useState<ReadonlySet<string>>(new Set());
  // Architect pairings saved in this sitting (#1085); the server's own record times are added below.
  const [pairingsSaved, setPairingsSaved] = useState<ReadonlySet<string>>(new Set());
  // The spans the pairing panel offers, drawn beside the item's own marks.
  const [pairMarks, setPairMarks] = useState<{ rowId: string; marks: Mark[]; active: string | null } | null>(null);
  const [slotVersion, setSlotVersion] = useState(0);
  const slots = useAsync(() => listSlotReaderRows(projectId, packageId), [projectId, packageId, slotVersion]);
  const base: LiveData = {
    rows: new Map(rows.map((row) => [row.row_id, row])),
    findings: new Map(findings.map((finding) => [finding.id, finding])),
    blocking,
    slots: slots.status === 'ready' ? new Map(slots.data.rows.map((s) => [s.row_id, s])) : null,
    wallsSaved,
  };

  // The list is taken once the readiness answer is in: without it the package-level checks would
  // be missing, and the queue could say "all done" while the server still blocks sign-off.
  const [items, setItems] = useState<QueueItem[] | null>(() => (blocking !== null && rowsReady ? buildQueue(rows, findings, blocking) : null));
  const [index, setIndex] = useState(() => (items ? firstOpen(items, base, startAt) : 0));
  if (items === null && blocking !== null && rowsReady) {
    const built = buildQueue(rows, findings, blocking);
    setItems(built);
    setIndex(firstOpen(built, base, startAt));
  }
  const list = items ?? [];

  // A reviewer's pairing recorded after a row's architect result (by the server's own times) waits
  // for a check run, across sittings, like a wall answer (#1085).
  const architectRowIds = list.filter((entry) => entry.kind === 'architect').map((entry) => entry.rowId).sort();
  const pairings = useAsync(
    async () => {
      const recorded = new Map<string, string>();
      await Promise.all(
        architectRowIds.map((rowId) =>
          getArchitectPairing(projectId, packageId, rowId).then(
            (answer) => {
              if (answer.current?.source === 'reviewer') recorded.set(rowId, answer.current.decided_at);
            },
            () => undefined,
          ),
        ),
      );
      return recorded;
    },
    [projectId, packageId, architectRowIds.join(','), pairingsSaved.size],
  );
  // The last known answer stays in force while it reloads, so a saved pairing never flickers back to
  // "open" (the same rule as corrections below).
  const [knownPairings, setKnownPairings] = useState<ReadonlyMap<string, string> | null>(null);
  if (pairings.status === 'ready' && pairings.data !== knownPairings) setKnownPairings(pairings.data);
  const pairedAfterResult = new Set<string>(pairingsSaved);
  const recordedPairings = (pairings.status === 'ready' ? pairings.data : knownPairings) ?? new Map<string, string>();
  for (const [rowId, decidedAt] of recordedPairings) {
    const architectFinding = base.rows.get(rowId)?.architect?.finding_id;
    const createdAt = architectFinding ? base.findings.get(architectFinding)?.created_at : undefined;
    if (createdAt && new Date(decidedAt).getTime() > new Date(createdAt).getTime()) pairedAfterResult.add(rowId);
  }

  // A finding with a correction anywhere in its history stays blocking until a new run, whatever
  // came after. Only findings that already have a recorded action can have one, so only they are asked.
  const suspects = list
    .map((item) => findingIdOf(item, base))
    .filter((id): id is string => id !== null && Boolean(base.findings.get(id)?.reviewer_action))
    .sort();
  const corrections = useAsync(
    async () => {
      const found = await Promise.all(
        suspects.map((id) =>
          listFindingActions(projectId, packageId, id).then(
            (history) => (history.items.some((action) => action.action === 'correct') ? id : null),
            () => null,
          ),
        ),
      );
      return new Set(found.filter((id): id is string => id !== null));
    },
    [projectId, packageId, suspects.join(',')],
  );
  // The last known answer stays in force while it reloads, so a correction is never briefly forgotten.
  const [knownCorrected, setKnownCorrected] = useState<ReadonlySet<string> | undefined>(undefined);
  if (corrections.status === 'ready' && corrections.data !== knownCorrected) setKnownCorrected(corrections.data);
  const live: LiveData = { ...base, corrected: corrections.status === 'ready' ? corrections.data : knownCorrected, pairingsSaved: pairedAfterResult };

  // Only "Go back through the items" keeps the list on screen once everything is handled.
  const [browsing, setBrowsing] = useState(false);
  const [savingWall, setSavingWall] = useState(false);
  const [changing, setChanging] = useState(false);
  const rules = useAsync(() => listRules(), []);
  const noteRef = useRef<HTMLTextAreaElement>(null);

  const item: QueueItem | undefined = list[index];
  const status: ItemStatus = item ? itemStatus(item, live) : 'decided';
  const row = item?.kind === 'countertop' ? live.rows.get(item.rowId) ?? null : null;
  // A split page (#1093): nothing was read, so it is checked or not checkable, never "Problem".
  const split = row !== null && isSplitPage(row);
  // "Matches the architect?" (#1085): the row it belongs to, and whether its pairing needs the reviewer.
  const architectRow = item?.kind === 'architect' ? live.rows.get(item.rowId) ?? null : null;
  const architect = architectRow?.architect ?? null;
  const architectAsk = architect ? architectState(architect) : null;
  const pairingAsked = item?.kind === 'architect' && status === 'open' && (architectAsk === 'confirm' || architectAsk === 'unpaired');
  const findingId = item ? findingIdOf(item, live) : null;
  const finding = findingId ? live.findings.get(findingId) ?? null : null;
  // A pairing question is answered by the pairing, never by the decision form.
  const deciding = finding !== null && (status === 'open' || changing) && !pairingAsked;
  const draft = useDecisionDraft(deciding ? finding : null);
  // A draft belongs to one result. If the result under this item is replaced (a check run finished
  // while the queue was open), the half-written choice and note are dropped, never re-aimed.
  const [draftFor, setDraftFor] = useState<string | null>(finding?.id ?? null);
  if ((finding?.id ?? null) !== draftFor) {
    setDraftFor(finding?.id ?? null);
    draft.reset();
    setChanging(false);
  }
  const progress = progressOf(list, live);
  const allHandled = list.length > 0 && progress.handled === progress.total;
  const unaccounted = unaccountedBlocking(list, live);
  // What a fresh list would add — only that can be loaded; anything else needs the results refreshed.
  const loadable = unaccounted && unaccounted.length > 0 && blocking !== null
    ? buildQueue(rows, findings, blocking).filter((candidate) => !list.some((item) => item.key === candidate.key)).length
    : 0;
  const slot = row && slots.status === 'ready' ? slots.data.rows.find((s) => s.row_id === row.row_id) ?? null : null;
  const question = status === 'open' ? wallQuestion(slot) : null;

  function go(to: number) {
    // Not while a save is in flight: its result belongs to the item it was made on.
    if (draft.saving || savingWall || to < 0 || to >= list.length) return;
    draft.reset();
    setChanging(false);
    setIndex(to);
  }

  async function saveDecision() {
    if (!deciding || !draft.ready) return;
    const from = index;
    if (await draft.save(handlers)) {
      setChanging(false);
      setBrowsing(false);
      // The page refreshed the results before resolving; move on to whatever is still open.
      const after = nextOpen(list, live, from, from);
      if (after !== null) setIndex(after);
    }
  }

  /** Start again from the server's current answer (new results arrived while the queue was open). */
  function reload() {
    const built = buildQueue(rows, findings, blocking);
    draft.reset();
    setChanging(false);
    setBrowsing(false);
    setItems(built);
    setIndex(firstOpen(built, live));
  }

  async function saveWall(rowId: string, config: string) {
    // The item stays put until the answer is saved or refused, so its outcome is always seen.
    setSavingWall(true);
    try {
      await reviewSlotReaderRow(projectId, packageId, rowId, { wall_config: config });
    } finally {
      setSavingWall(false);
    }
    setWallsSaved((current) => new Set(current).add(rowId));
    setBrowsing(false);
    setSlotVersion((n) => n + 1);
    onWallSaved();
  }

  const onKeyDown = useEffectEvent((event: KeyboardEvent) => {
    if (event.defaultPrevented || event.altKey) return;
    const inField = event.target instanceof Element && event.target.closest(IN_FIELD) !== null;
    if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
      event.preventDefault();
      void saveDecision();
      return;
    }
    if (inField || event.metaKey || event.ctrlKey) return;
    const keys: Record<string, () => void> = {
      j: () => go(index + 1),
      k: () => go(index - 1),
      ...(deciding
        ? {
            '1': () => draft.choose('confirm'),
            '2': () => { if (!split) draft.choose('problem'); },
            '3': () => draft.choose('dismiss'),
            n: () => noteRef.current?.focus(),
            Enter: () => void saveDecision(),
          }
        : {}),
    };
    const action = keys[event.key] ?? keys[event.key.toLowerCase()];
    if (!action) return;
    // A button that has focus answers Enter itself.
    if (event.key === 'Enter' && event.target instanceof HTMLButtonElement) return;
    event.preventDefault();
    action();
  });
  useEffect(() => {
    const listener = (event: KeyboardEvent) => onKeyDown(event);
    window.addEventListener('keydown', listener);
    return () => window.removeEventListener('keydown', listener);
  }, []);

  const ruleName = (id: string) => (rules.status === 'ready' ? rules.data.find((r) => r.rule_id === id)?.name : undefined) ?? id;
  // An architect item looks like its architect result on the drawing, never like the width's.
  const target = row ? targetFromRow(row) : architectRow ? targetFromArchitect(architectRow) : finding ? targetFromFinding(finding) : null;
  // The offered spans are drawn only while the pairing question is on screen.
  const pairing = pairingAsked && pairMarks && architectRow && pairMarks.rowId === architectRow.row_id ? pairMarks : null;
  const extraMarks = pairing?.marks ?? [];

  return (
    <TooltipProvider delayDuration={250}>
      <div data-tw data-slot="needs-you-queue" tabIndex={-1} className="flex h-full min-h-0 flex-col font-sans text-foreground outline-none">
        <header className="flex flex-wrap items-center gap-x-4 gap-y-2 border-b px-4 py-3">
          <div className="flex min-w-0 items-center gap-2">
            <DialogTitle className="text-base font-semibold">Needs you</DialogTitle>
            <DialogDescription className="sr-only">Decide the items still blocking sign-off, one at a time.</DialogDescription>
          </div>
          <div className="flex min-w-48 flex-1 items-center gap-3" data-slot="queue-progress">
            <Progress value={list.length ? (progress.handled / progress.total) * 100 : 100} className="h-2 flex-1" aria-label="Items handled" />
            <span className="num shrink-0 text-sm">
              {progress.handled} of {progress.total}
            </span>
          </div>
          <div className="flex items-center gap-1">
            <Button size="sm" variant="outline" onClick={() => go(index - 1)} disabled={draft.saving || savingWall || index <= 0 || list.length === 0} aria-label="Previous item (K)">
              <ChevronLeft /> <kbd className="num text-xs text-muted-foreground">K</kbd>
            </Button>
            <span className="num w-14 text-center text-xs text-muted-foreground" aria-live="polite">
              {list.length ? `${index + 1} / ${list.length}` : '—'}
            </span>
            <Button size="sm" variant="outline" onClick={() => go(index + 1)} disabled={draft.saving || savingWall || index >= list.length - 1} aria-label="Next item (J)">
              <kbd className="num text-xs text-muted-foreground">J</kbd> <ChevronRight />
            </Button>
            <DialogClose asChild>
              <Button size="icon-sm" variant="ghost" aria-label="Close the queue">
                <X />
              </Button>
            </DialogClose>
          </div>
        </header>

        {items === null ? (
          <div className="m-auto flex max-w-sm flex-col items-center gap-1 p-6 text-center" role="status">
            <p className="text-sm">Loading what needs you…</p>
            <p className="text-xs text-muted-foreground">If this does not finish, close the queue and refresh the results.</p>
          </div>
        ) : (list.length === 0 || (allHandled && !browsing)) && unaccounted !== null && unaccounted.length > 0 ? (
          // The server still blocks on results this list does not hold: never claim "all done".
          <div data-slot="queue-changed" className="m-auto flex max-w-md flex-col items-center gap-3 p-6 text-center" role="status">
            <p className="text-lg font-semibold">{list.length === 0 ? 'Items need you' : 'The results changed'}</p>
            {loadable > 0 ? (
              <>
                <p className="text-sm text-muted-foreground">
                  <span className="num">{loadable}</span> more {loadable === 1 ? 'item needs' : 'items need'} you.
                </p>
                <Button onClick={reload}>Load them</Button>
              </>
            ) : (
              <p className="text-sm text-muted-foreground">
                <span className="num">{unaccounted.length}</span> {unaccounted.length === 1 ? 'result still blocks' : 'results still block'} sign-off but {unaccounted.length === 1 ? 'is' : 'are'} not in the results on screen yet. Close the queue and refresh the results.
              </p>
            )}
          </div>
        ) : (list.length === 0 || (allHandled && !browsing)) && unaccounted === null ? (
          <p className="m-auto p-6 text-sm text-muted-foreground" role="status">
            Checking what is left…
          </p>
        ) : list.length === 0 || (allHandled && !browsing) ? (
          <DoneState
            empty={list.length === 0}
            waitingForRun={progress.waitingForRun}
            next={next}
            onAct={(kind) => {
              onOpenChange(false);
              onAct(kind);
            }}
            onBrowse={() => {
              draft.reset();
              setChanging(false);
              setIndex(0);
              setBrowsing(true);
            }}
          />
        ) : item ? (
          <>
            {/* A check run makes new results, and a decision belongs to the result it was made on. */}
            {progress.waitingForRun > 0 && status === 'open' && !question && (
              <p role="note" data-slot="queue-run-first" className="border-b bg-muted/50 px-4 py-2 text-sm">
                <span className="num">{progress.waitingForRun}</span> {progress.waitingForRun === 1 ? 'answer waits' : 'answers wait'} for a check run. That run replaces these results: a decision made now carries over only if its result comes back unchanged, so run the checks first.
              </p>
            )}
            <div className="min-h-0 flex-1 overflow-y-auto lg:grid lg:grid-cols-[minmax(0,27rem)_minmax(0,1fr)] lg:overflow-hidden" data-slot="queue-item" data-status={status}>
              <section aria-label="This item" className="flex flex-col gap-4 p-4 lg:overflow-y-auto lg:border-r">
                <div className="flex flex-col gap-1.5">
                  {/* A package-level check is named by its rule; its scope ("Package revision") goes below. */}
                  <h2 className="text-lg font-semibold leading-tight">
                    {item.kind === 'check' && finding ? ruleName(finding.check_id) : item.kind === 'architect' ? `${item.label}: matches the architect?` : item.label}
                  </h2>
                  <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                    {item.kind === 'architect' ? architect && <ArchitectStatus result={architect} /> : target && <TonePill target={target} />}
                    {/* As in the results table: "Needs you" beside a result that does not already say so. */}
                    {status === 'open' && item.kind !== 'architect' && target?.glyph !== 'REVIEW_REQUIRED' && (
                      <span className="inline-flex items-center gap-1 font-medium text-outcome-review-fg">
                        <OutcomeIcon outcome="REVIEW_REQUIRED" size={12} /> Needs you
                      </span>
                    )}
                    {item.page !== null && <span className="num">Page {item.page}</span>}
                    {item.kind === 'check' && <span>{item.label}</span>}
                  </div>
                </div>

                {/* A split page (#1093): no line was chosen, so no picture and no numbers, only why. */}
                {row?.hold && isSplitPage(row) ? (
                  <SplitPageNote hold={row.hold} />
                ) : row && (
                  <>
                    <CountertopStrip row={row} size="full" />
                    <Facts row={row} />
                  </>
                )}
                {row && <ArchitectLine result={row.architect} />}
                {item.kind === 'architect' && architect && (
                  <div className="flex flex-col gap-2" data-slot="queue-architect">
                    <ArchitectPairs result={architect} />
                    {pairedByWords(architect) && <p className="text-xs text-muted-foreground">{pairedByWords(architect)}</p>}
                    {architect.reason && <p className="text-sm text-muted-foreground">{architect.reason}</p>}
                    {target && target.marks.length > 0 && <MarkNotes marks={target.marks} at={target} />}
                  </div>
                )}
                {item.kind === 'architect' && !architectRow && (
                  <p className="rounded-lg border border-dashed p-3 text-sm text-muted-foreground">This countertop is not in the current results.</p>
                )}
                {pairingAsked && architectRow && (
                  <ArchitectPairingPanel
                    key={architectRow.row_id}
                    projectId={projectId}
                    packageId={packageId}
                    row={architectRow}
                    canConfirm={architectAsk === 'confirm'}
                    onSaved={() => {
                      setPairingsSaved((current) => new Set(current).add(architectRow.row_id));
                      setBrowsing(false);
                      onPairingSaved?.();
                    }}
                    onMarks={(marks, active) => setPairMarks({ rowId: architectRow.row_id, marks, active })}
                  />
                )}
                {item.kind === 'check' && finding?.reason && <p className="line-clamp-3 text-sm text-muted-foreground" title={finding.reason}>{finding.reason}</p>}
                {!(target && target.page !== null) && <p className="text-xs text-muted-foreground lg:hidden">No drawing location for this item.</p>}

                {item.kind === 'countertop' && !row && (
                  <p className="rounded-lg border border-dashed p-3 text-sm text-muted-foreground">This countertop is not in the current results.</p>
                )}
                {question && row && (
                  <WallChoices
                    key={question.rowId}
                    question={question}
                    onSave={saveWall}
                    onOpenCard={() => {
                      onOpenChange(false);
                      onOpenCard(row);
                    }}
                  />
                )}

                {status !== 'open' && !changing && (
                  <DecidedSummary
                    status={status}
                    wallSaved={row !== null && wallsSaved.has(row.row_id)}
                    pairingSaved={item.kind === 'architect' && live.pairingsSaved?.has(item.rowId) === true}
                    corrected={finding !== null && (live.corrected?.has(finding.id) ?? false)}
                    row={row}
                    finding={finding}
                    outcome={item.kind === 'architect' ? architect?.outcome ?? finding?.outcome ?? null : undefined}
                    projectId={projectId}
                    packageId={packageId}
                    onChange={finding && status === 'decided' ? () => setChanging(true) : undefined}
                  />
                )}

                {status === 'open' && !finding && split && (
                  <p data-slot="split-unchecked" className="rounded-lg border border-dashed p-3 text-sm">Not checked yet. Run the checks; then this page needs your decision.</p>
                )}
                {status === 'open' && !finding && row && !question && !split && (
                  <div className="flex flex-col items-start gap-2 rounded-lg border border-dashed p-3 text-sm">
                    <p>Not checked yet. Its missing widths or walls are filled in its countertop card; then run the checks.</p>
                    <Button size="sm" variant="outline" onClick={() => { onOpenChange(false); onOpenCard(row); }}>
                      Open countertop card
                    </Button>
                  </div>
                )}
              </section>

              {/* On a phone an item with no location gets a line, not an empty panel. */}
              <section aria-label="Drawing" className={cn('flex h-[55dvh] min-h-72 flex-col border-t lg:h-auto lg:min-h-0 lg:border-t-0', !(target && target.page !== null) && 'max-lg:hidden')}>
                {target && target.page !== null ? (
                  <DrawingViewer
                    key={item.key}
                    embedded
                    target={target}
                    rows={rows}
                    projectId={projectId}
                    packageId={packageId}
                    onTargetChange={() => undefined}
                    extraMarks={extraMarks}
                    activeMark={pairing?.active ?? null}
                  />
                ) : (
                  <p className="m-auto p-6 text-sm text-muted-foreground">No drawing location for this item.</p>
                )}
              </section>
            </div>

            {deciding && (
              <form
                data-slot="queue-decision"
                className="flex shrink-0 flex-col gap-3 border-t bg-background p-4"
                onSubmit={(event) => {
                  event.preventDefault();
                  void saveDecision();
                }}
              >
                <DecisionFields draft={draft} idPrefix="queue" big noteRef={noteRef} allowProblem={!split} />
                <div className="flex flex-wrap items-center gap-2">
                  <Button type="submit" disabled={!draft.ready || draft.saving}>
                    {draft.saving ? 'Saving…' : 'Record decision'}
                    <kbd className="num ml-1 hidden text-xs opacity-70 sm:inline">Enter</kbd>
                  </Button>
                  {changing && (
                    <Button type="button" variant="ghost" onClick={() => { draft.reset(); setChanging(false); }}>
                      Keep the decision
                    </Button>
                  )}
                  <span className="ml-auto hidden text-xs text-muted-foreground sm:inline">1 2 3 choose · N note · J K move</span>
                </div>
              </form>
            )}
          </>
        ) : null}
      </div>
    </TooltipProvider>
  );
}

/**
 * Three or four facts as chips: printed, needed, the difference, the walls; then the row's "drawn
 * length not checked" note when the API has one (#1107).
 */
function Facts({ row }: { row: CountertopResult }) {
  const { text, sign } = formatDelta(row.delta);
  return (
    <div className="flex flex-col gap-2">
      <dl data-slot="queue-facts" className="flex flex-wrap gap-2 text-xs">
        <Chip term="Printed" value={row.printed_overall?.display ?? '—'} />
        <Chip term="Needed" value={row.expected_total?.display ?? '—'} />
        <div className="flex items-center gap-1.5 rounded-full border px-2.5 py-1">
          <dt className="text-muted-foreground">Difference</dt>
          <dd className={cn('num inline-flex items-center gap-1 font-medium', sign === null ? 'text-muted-foreground' : sign === 0 ? 'text-outcome-pass-fg' : 'text-outcome-fail-fg')}>
            {sign !== null && <OutcomeIcon outcome={sign === 0 ? 'PASS' : 'FAIL'} size={12} />}
            {sign === null ? '—' : text}
          </dd>
        </div>
        <div className="flex items-center gap-1.5 rounded-full border px-2.5 py-1">
          <dt className="text-muted-foreground">Walls</dt>
          <dd>
            <WallGlyph layout={row.wall_layout} labelled />
          </dd>
        </div>
      </dl>
      <DrawnLengthNote row={row} />
    </div>
  );
}

function Chip({ term, value }: { term: string; value: string }) {
  return (
    <div className="flex items-center gap-1.5 rounded-full border px-2.5 py-1">
      <dt className="text-muted-foreground">{term}</dt>
      <dd className="num font-medium">{value}</dd>
    </div>
  );
}

/**
 * The wall question, as on the countertop card (#1018/#1025): the published choices, nothing
 * pre-selected, the readers' proposal as text, and a save only on the reviewer's click. Keyed by row,
 * so a choice made on one countertop can never carry over to the next.
 */
function WallChoices({ question, onSave, onOpenCard }: { question: WallQuestion; onSave: (rowId: string, config: string) => Promise<void>; onOpenCard: () => void }) {
  const [choice, setChoice] = useState('');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function save(config: string) {
    setSaving(true);
    setError(null);
    try {
      await onSave(question.rowId, config);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setSaving(false);
    }
  }

  return (
    <section aria-labelledby="queue-walls" data-slot="queue-walls" className="flex flex-col gap-2 rounded-lg border p-3">
      <h3 id="queue-walls" className="text-sm font-medium">
        Walls for this countertop
      </h3>
      {question.betweenPanels && (
        <>
          {/* The server's own reason says it; a sentence of ours only when it does not. */}
          {!question.reason && <p className="text-sm text-muted-foreground">The stone sits between side panels, so the panels take the field cut.</p>}
          <Button size="sm" className="self-start" disabled={saving} onClick={() => void save('back_only')}>
            Back only: stone between panels
          </Button>
          <p className="text-xs text-muted-foreground">Or choose another layout:</p>
        </>
      )}
      {!question.betweenPanels && question.proposal && (
        <p className="text-sm text-muted-foreground">The readers propose: {wallWords(question.proposal).toLowerCase()}. Not confirmed.</p>
      )}
      <ToggleGroup type="single" variant="outline" size="sm" value={choice} onValueChange={setChoice} aria-label="Wall layout" className="flex-wrap justify-start">
        {question.choices.map((config) => (
          <ToggleGroupItem key={config} value={config} className="gap-1.5 px-2.5">
            <WallLayoutPicture config={config} />
            {wallWords(config)}
          </ToggleGroupItem>
        ))}
      </ToggleGroup>
      <div className="flex flex-wrap items-center gap-2">
        <Button size="sm" variant={question.betweenPanels ? 'outline' : 'default'} disabled={!choice || saving} onClick={() => void save(choice)}>
          Use this wall layout
        </Button>
        <Button size="sm" variant="ghost" onClick={onOpenCard}>
          Open countertop card
        </Button>
      </div>
      {question.reason && <p className="text-xs text-muted-foreground">{question.reason}</p>}
      <p className="text-xs text-muted-foreground">The result changes only after the checks run again.</p>
      {error && (
        <p role="alert" className="text-sm text-outcome-fail-fg">
          {error}
        </p>
      )}
    </section>
  );
}

/** What was decided, by whom, and a way to change it (a new action; the latest counts). */
function DecidedSummary({
  status,
  wallSaved,
  pairingSaved = false,
  corrected,
  row,
  finding,
  outcome: givenOutcome,
  projectId,
  packageId,
  onChange,
}: {
  status: ItemStatus;
  /** A wall answer saved in this sitting. */
  wallSaved: boolean;
  /** An architect pairing recorded after the result (#1085). */
  pairingSaved?: boolean;
  /** The result to word the decision against, when it is not the row's own (the architect's). */
  outcome?: CountertopResult['outcome'];
  /** A correction somewhere in this finding's history (only a new run clears it). */
  corrected: boolean;
  row: CountertopResult | null;
  finding: Finding | null;
  projectId: string;
  packageId: string;
  onChange?: () => void;
}) {
  const decision = row?.reviewer_decision ?? (finding?.reviewer_action ? { action: finding.reviewer_action, actor: finding.reviewed_by ?? '', note: finding.reviewer_note ?? null, carried_over: finding.reviewer_carried_over } : null);
  const outcome = givenOutcome !== undefined ? givenOutcome : row?.outcome ?? finding?.outcome ?? null;
  return (
    <section data-slot="queue-decided" className="flex flex-col gap-2 rounded-lg border p-3 text-sm">
      {status === 'waiting-for-run' ? (
        <p className="flex items-center gap-1.5 font-medium">
          <CheckCircle2 className="size-4" aria-hidden="true" />
          {decision?.action === 'correct' || corrected
            ? 'Corrected. A correction is settled only by running the checks again; nothing recorded after it clears it.'
            : pairingSaved
              ? 'Pairing saved. It counts once the checks run again.'
              : wallSaved
              ? 'Wall answer saved. Run the checks to see the result.'
              : 'Its walls or widths were saved after this result. Run the checks to see the new result.'}
        </p>
      ) : (
        <p className="flex items-center gap-1.5 font-medium">
          {/* Neutral: a decision is the reviewer's, not a PASS (only outcomes are coloured). */}
          <CheckCircle2 className="size-4" aria-hidden="true" />
          {decision ? `Decided: ${decisionWords(decision.action, outcome)}` : 'No longer blocking sign-off'}
          {decision?.actor && <span className="font-normal text-muted-foreground">by {decision.actor}</span>}
          {decision?.carried_over && <CarriedOver />}
        </p>
      )}
      {decision?.note && <p className="text-muted-foreground">“{decision.note}”</p>}
      <div className="flex flex-wrap gap-2">
        {onChange && (
          <Button size="sm" variant="outline" onClick={onChange}>
            Change decision
          </Button>
        )}
        {finding && <HistoryButton findingId={finding.id} outcome={outcome} projectId={projectId} packageId={packageId} />}
      </div>
    </section>
  );
}

function HistoryButton({ findingId, outcome, projectId, packageId }: { findingId: string; outcome: CountertopResult['outcome']; projectId: string; packageId: string }) {
  const [open, setOpen] = useState(false);
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button size="sm" variant="ghost">
          <History /> History
        </Button>
      </PopoverTrigger>
      <PopoverContent className="w-80 font-sans" align="start">
        {open && <HistoryList findingId={findingId} outcome={outcome} projectId={projectId} packageId={packageId} />}
      </PopoverContent>
    </Popover>
  );
}

function HistoryList({ findingId, outcome, projectId, packageId }: { findingId: string; outcome: CountertopResult['outcome']; projectId: string; packageId: string }) {
  const history = useAsync(() => listFindingActions(projectId, packageId, findingId), [projectId, packageId, findingId]);
  if (history.status === 'loading') return <p className="text-sm text-muted-foreground" role="status">Loading the history…</p>;
  if (history.status === 'error') return <p className="text-sm" role="alert">The history could not be loaded: {history.error.message}</p>;
  if (history.data.items.length === 0) return <p className="text-sm text-muted-foreground">No decisions recorded yet.</p>;
  return (
    <ol data-slot="queue-history" aria-label="Decisions, newest first" className="flex flex-col gap-2 text-sm">
      {history.data.items.map((action, i) => (
        <li key={action.id} className={cn('flex flex-col gap-0.5', i > 0 && 'text-muted-foreground')}>
          <span className="font-medium">
            {decisionWords(action.action, outcome)}
            {action.carried_over && <> <CarriedOver /></>}
            {i === 0 && <span className="ml-1.5 rounded-full border px-1.5 text-xs font-normal">latest</span>}
          </span>
          <span className="text-xs">
            {action.actor} · <span className="num">{new Date(action.created_at).toLocaleString()}</span>
          </span>
          {action.note && <span className="text-xs">“{action.note}”</span>}
        </li>
      ))}
    </ol>
  );
}

function DoneState({ empty, waitingForRun, next, onAct, onBrowse }: { empty: boolean; waitingForRun: number; next: NextAction; onAct: (kind: NextActionKind) => void; onBrowse: () => void }) {
  return (
    <div data-slot="queue-done" className="m-auto flex max-w-md flex-col items-center gap-3 p-6 text-center" role="status">
      <CheckCircle2 className="size-10 text-outcome-pass-fg" aria-hidden="true" />
      <p className="text-lg font-semibold">{empty ? 'Nothing needs you' : 'All decisions made'}</p>
      {waitingForRun > 0 ? (
        <>
          <p className="text-sm text-muted-foreground">
            <span className="num">{waitingForRun}</span> {waitingForRun === 1 ? 'answer needs' : 'answers need'} a new check run before {waitingForRun === 1 ? 'it shows' : 'they show'} in the results. The run replaces the current results; a decision carries over only where its result comes back unchanged.
          </p>
          <Button onClick={() => onAct('run-checks')}>Run checks</Button>
        </>
      ) : (
        next.kind !== 'review' && (
          <>
            <Button disabled={next.disabled} onClick={() => onAct(next.kind)}>
              {next.label}
            </Button>
            {next.disabled && next.reason && <p className="text-xs text-muted-foreground">{next.reason}</p>}
          </>
        )
      )}
      {!empty && (
        <Button variant="ghost" size="sm" onClick={onBrowse}>
          Go back through the items
        </Button>
      )}
    </div>
  );
}
