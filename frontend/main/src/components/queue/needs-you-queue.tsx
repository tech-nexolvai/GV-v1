import { useEffect, useEffectEvent, useRef, useState } from 'react';
import { CheckCircle2, ChevronLeft, ChevronRight, History, X } from 'lucide-react';

import { getArchitectPairing, listFindingActions, listRules, listSlotReaderRows, reviewSlotReaderRow, type CountertopResult, type PageWithoutCountertop } from '@/api/client';
import { pageTarget } from '@/lib/assistant';
import { THERE_IS_A_COUNTERTOP_TIP, type AnswerNoCountertop } from '@/components/results/no-countertop-pages';
import { useAsync } from '@/api/useAsync';
import type { Finding } from '@/data/types';
import { countertopNotes } from '@/lib/countertop-notes';
import { decisionWords, formatDelta, isNotChecked, isSplitPage, NO_RESULT_HINT, notCheckedSituation, notCheckedSummary, sharedNotComparedReason } from '@/lib/countertop-results';
import { targetFromArchitect, targetFromFinding, targetFromRow, type Mark } from '@/lib/drawing-viewer';
import { architectState, pairedByWords } from '@/lib/architect';
import {
  buildQueue,
  itemStatus,
  nextOpen,
  progressOf,
  findingIdOf,
  unaccountedBlocking,
  viewPickWaits,
  wallQuestion,
  wallWords,
  BETWEEN_PANELS_WORDS,
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
import { ArchitectLine, ArchitectPairs, ArchitectSharedLine, ArchitectStatus, ArchitectViewLink, MatchTag } from '@/components/results/architect-line';
import { CountertopTitle } from '@/components/results/countertop-title';
import { ArchitectPairingPanel } from './architect-pairing';
import { ArchitectViewPicker } from './architect-view-picker';
import { DecisionFields } from '@/components/results/decision-form';
import { useDecisionDraft, type DecideHandlers } from '@/components/results/use-decision-draft';
import { WallGlyph, WallLayoutPicture } from '@/components/results/wall-glyph';
import { WallChoiceResult, WallRuleInfo, WallsLine } from '@/components/results/wall-rule-info';
import { DrawnLengthNote } from '@/components/results/drawn-length-note';
import { NeedsValues } from '@/components/results/needs-values';
import { DrawingViewer, MarkNotes } from '@/components/drawing/drawing-viewer';
import { TonePill } from '@/components/drawing/page-canvas';
import { OutcomeIcon } from '@/components/ui/OutcomeIcon';
import { Button } from '@/components/ui/button';
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import { Progress } from '@/components/ui/progress';
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group';
import { TooltipProvider } from '@/components/ui/tooltip';
import { useMediaQuery } from '@/hooks/use-media-query';
import { SIGNED_OFF_NOTE, useSignedOff } from '@/lib/signed-off';

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
  /** An architect view was chosen for this row (#1168): likewise, only a new check run uses it. */
  onViewPicked?: (rowId: string) => void;
  onOpenCard: (row: CountertopResult) => void;
  /** Open at this item (its key), when it is in the list; otherwise at the first open one. */
  startAt?: string | null;
  /** A value typed where a waiting check asked for it (#1181): only a new check run uses it. */
  onValuesSaved?: () => void;
  /**
   * Pages listed with no countertop that sign-off still waits on (#1191), from readiness. They are
   * answered on Results, so the queue names them instead of claiming that nothing is left.
   */
  unconfirmedNoCountertopPages?: readonly number[];
  onShowNoCountertopPages?: () => void;
  /** Pages listed with no countertop (#1191): each unconfirmed one is an item the queue walks. */
  pagesWithoutCountertop?: readonly PageWithoutCountertop[];
  /** Records "Confirm: no countertop here" / "There is a countertop" on one of them. */
  onAnswerNoCountertop?: AnswerNoCountertop;
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
/** No fine pointer of any kind: a touch-only screen, where key hints would only be noise. */
const TOUCH_ONLY = 'not all and (any-pointer: fine)';

/** A reason shown on its own starts with a capital, whatever case the record keeps (display only). */
function sentence(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function firstOpen(items: readonly QueueItem[], live: LiveData, startAt?: string | null): number {
  const asked = startAt ? items.findIndex((item) => item.key === startAt) : -1;
  if (asked >= 0) return asked;
  return Math.max(0, items.findIndex((item) => itemStatus(item, live) === 'open'));
}

function QueueBody({ rows, rowsReady, findings, blocking, projectId, packageId, handlers, next, onAct, onOpenChange, onWallSaved, onPairingSaved, onViewPicked, onOpenCard, startAt, onValuesSaved, unconfirmedNoCountertopPages = [], onShowNoCountertopPages, pagesWithoutCountertop = [], onAnswerNoCountertop }: QueueProps) {
  const signedOff = useSignedOff();
  const [wallsSaved, setWallsSaved] = useState<ReadonlySet<string>>(new Set());
  // Architect pairings saved in this sitting (#1085); the server's own record times are added below.
  const [pairingsSaved, setPairingsSaved] = useState<ReadonlySet<string>>(new Set());
  // Architect views chosen in this sitting (#1168); the server's own `waits_for_run` is read from the rows.
  const [viewsPicked, setViewsPicked] = useState<ReadonlySet<string>>(new Set());
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
    viewsPicked,
    pages: new Map(pagesWithoutCountertop.filter((page) => page.record_id).map((page) => [page.record_id!, page])),
  };

  // The list is taken once the readiness answer is in: without it the package-level checks would
  // be missing, and the queue could say "all done" while the server still blocks sign-off.
  const [items, setItems] = useState<QueueItem[] | null>(() => (blocking !== null && rowsReady ? buildQueue(rows, findings, blocking, pagesWithoutCountertop) : null));
  const [index, setIndex] = useState(() => (items ? firstOpen(items, base, startAt) : 0));
  if (items === null && blocking !== null && rowsReady) {
    const built = buildQueue(rows, findings, blocking, pagesWithoutCountertop);
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
  // Key hints only where a keyboard is likely (#1155): a fine pointer (mouse, trackpad) anywhere.
  // A touch-only screen has no J / K / Enter to press; a tablet with a trackpad keeps the hints.
  const touch = useMediaQuery(TOUCH_ONLY);
  // Said once for every countertop when they all share it (#1126), as on Results.
  const sharedReason = sharedNotComparedReason(rows);

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
  // "Which of the architect's views?" (#1168): answered by the view picker, never by the decision form.
  const viewAsked = item?.kind === 'architect' && status === 'open' && architectAsk === 'choose-view';
  // Never matched with a view (#1168): only a new check run answers it, not a decision.
  const runAgain = item?.kind === 'architect' && status === 'open' && architectAsk === 'run-again';
  const findingId = item ? findingIdOf(item, live) : null;
  const finding = findingId ? live.findings.get(findingId) ?? null : null;
  // A pairing question is answered by the pairing, never by the decision form.
  const deciding = finding !== null && (status === 'open' || changing) && !pairingAsked && !viewAsked && !runAgain;
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
    ? buildQueue(rows, findings, blocking, pagesWithoutCountertop).filter((candidate) => !list.some((item) => item.key === candidate.key)).length
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
    if (!deciding || !draft.ready || signedOff) return;
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
    const built = buildQueue(rows, findings, blocking, pagesWithoutCountertop);
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
  // A page listed with no countertop (#1191) shows its page: nothing on it was outlined.
  const listedPage = item?.kind === 'page' ? live.pages?.get(item.recordId) ?? null : null;
  const target = row
    ? targetFromRow(row)
    : architectRow
      ? targetFromArchitect(architectRow)
      : finding
        ? targetFromFinding(finding)
        : item?.kind === 'page'
          ? pageTarget(item.page, { rows, findings, pagesWithoutCountertop })
          : null;
  // The offered spans are drawn only while the pairing question is on screen.
  const pairing = pairingAsked && pairMarks && architectRow && pairMarks.rowId === architectRow.row_id ? pairMarks : null;
  const extraMarks = pairing?.marks ?? [];

  return (
    <TooltipProvider delayDuration={250}>
      <div data-tw data-slot="needs-you-queue" tabIndex={-1} className="flex h-full min-h-0 flex-col font-sans text-foreground outline-none">
        {/* On a phone: the title, the position and close on one line, the progress on the next. */}
        <header data-slot="queue-header" className="flex flex-wrap items-center gap-x-4 gap-y-2 border-b px-4 py-3">
          <div className="flex min-w-0 flex-1 items-center gap-2 sm:flex-none">
            <DialogTitle className="text-base font-semibold">Needs you</DialogTitle>
            <DialogDescription className="sr-only">Decide the items still blocking sign-off, one at a time.</DialogDescription>
          </div>
          <div className="order-last flex min-w-48 basis-full items-center gap-3 sm:order-none sm:basis-auto sm:flex-1" data-slot="queue-progress">
            <Progress value={list.length ? (progress.handled / progress.total) * 100 : 100} className="h-2 flex-1" aria-label="Items handled" />
            {/* The count with its noun (#1179): items that can be decided, never rows not checked yet. */}
            <span className="shrink-0 text-sm">
              <span className="num">{progress.handled}</span> of <span className="num">{progress.total}</span> {progress.total === 1 ? 'item' : 'items'}
            </span>
          </div>
          <div className="flex items-center gap-1" data-slot="queue-position">
            {/* Keys only where there is a keyboard to press them (#1155): not on a touch screen. */}
            <Button size="sm" variant="outline" onClick={() => go(index - 1)} disabled={draft.saving || savingWall || index <= 0 || list.length === 0} aria-label={touch ? 'Previous item' : 'Previous item (K)'}>
              <ChevronLeft />
              {!touch && <kbd className="num text-xs text-muted-foreground">K</kbd>}
            </Button>
            <span className="num w-14 text-center text-xs text-muted-foreground" aria-live="polite">
              {list.length ? `${index + 1} / ${list.length}` : '—'}
            </span>
            <Button size="sm" variant="outline" onClick={() => go(index + 1)} disabled={draft.saving || savingWall || index >= list.length - 1} aria-label={touch ? 'Next item' : 'Next item (J)'}>
              {!touch && <kbd className="num text-xs text-muted-foreground">J</kbd>}
              <ChevronRight />
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
            notChecked={rows.filter(isNotChecked).length}
            checksHaveRun={findings.length > 0}
            waitingForRun={progress.waitingForRun}
            unconfirmedPages={unconfirmedNoCountertopPages}
            onShowPages={onShowNoCountertopPages}
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
                  {/* A countertop says its page once, in its name (#1155). */}
                  <h2 className="text-lg font-semibold leading-tight">
                    {item.kind === 'page' ? (
                      <>Page <span className="num">{item.page}</span>: no countertop?</>
                    ) : item.kind === 'check' ? (finding ? ruleName(finding.check_id) : item.label) : (
                      <>
                        <CountertopTitle row={{ row_id: item.rowId, page_number: item.page, label: item.label }} rows={rows} />
                        {item.kind === 'architect' && ': matches the architect?'}
                      </>
                    )}
                  </h2>
                  <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                    {item.kind === 'architect' ? architect && <ArchitectStatus result={architect} /> : target && <TonePill target={target} />}
                    {/* As in the results table: "Needs you" beside a result that does not already say so. */}
                    {status === 'open' && item.kind !== 'architect' && target?.glyph !== 'REVIEW_REQUIRED' && (
                      <span className="inline-flex items-center gap-1 font-medium text-outcome-review-fg">
                        <OutcomeIcon outcome="REVIEW_REQUIRED" size={12} /> Needs you
                      </span>
                    )}
                    {item.kind === 'check' && item.page !== null && <span className="num">Page {item.page}</span>}
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
                {/* Every countertop shares one "not compared" reason: one short line, as Results says it (#1155). */}
                {row && (sharedReason !== null && architectState(row.architect ?? null) === 'not-compared' ? <ArchitectSharedLine reason={sharedReason} /> : <ArchitectLine result={row.architect} />)}
                {item.kind === 'architect' && architect && (
                  <div className="flex flex-col gap-2" data-slot="queue-architect">
                    <ArchitectPairs result={architect} />
                    {pairedByWords(architect) && <p className="text-xs text-muted-foreground">{pairedByWords(architect)}</p>}
                    {/* The view it was matched with (#1168); "Architect's view" on the drawing shows it beside. */}
                    <ArchitectViewLink result={architect} />
                    <MatchTag result={architect} />
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
                {runAgain && (
                  <p data-slot="queue-run-again" className="rounded-lg border border-dashed p-3 text-sm">
                    This countertop has not been matched with a view of the architect&apos;s drawings yet. Run the checks again.
                  </p>
                )}
                {viewAsked && architectRow && (
                  <ArchitectViewPicker
                    key={architectRow.row_id}
                    projectId={projectId}
                    packageId={packageId}
                    row={architectRow}
                    onSaved={() => {
                      setViewsPicked((current) => new Set(current).add(architectRow.row_id));
                      setBrowsing(false);
                      onViewPicked?.(architectRow.row_id);
                    }}
                  />
                )}
                {item.kind === 'check' && finding?.reason && <p className="line-clamp-3 text-sm text-muted-foreground" title={finding.reason}>{finding.reason}</p>}
                {item.kind === 'check' && finding && countertopNotes(finding).map((note) => (
                  <p key={note} data-slot="countertop-note" className="text-sm text-muted-foreground">{note}</p>
                ))}
                {item.kind === 'page' && (
                  <PageQuestion
                    key={item.recordId}
                    page={listedPage}
                    status={status}
                    onAnswer={onAnswerNoCountertop}
                    onAnswered={() => {
                      setBrowsing(false);
                      // The page reloaded the results before resolving; move on to whatever is still open.
                      const after = nextOpen(list, live, index, index);
                      if (after !== null) setIndex(after);
                    }}
                  />
                )}
                {/* A check waiting on a value asks for exactly that value, here (#1181). */}
                {item.kind === 'check' && finding && status === 'open' && ((finding.missing_inputs?.length ?? 0) > 0 || (finding.depth_readings?.length ?? 0) > 0) && (
                  <NeedsValues
                    finding={finding}
                    packageId={packageId}
                    onSaved={() => onValuesSaved?.()}
                    onRunChecks={() => {
                      onOpenChange(false);
                      onAct('run-checks');
                    }}
                  />
                )}
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

                {status !== 'open' && !changing && item.kind !== 'page' && (
                  <DecidedSummary
                    status={status}
                    wallSaved={row !== null && wallsSaved.has(row.row_id)}
                    pairingSaved={item.kind === 'architect' && live.pairingsSaved?.has(item.rowId) === true}
                    viewPicked={item.kind === 'architect' && viewPickWaits(item.rowId, architectRow ?? undefined, live)}
                    corrected={finding !== null && (live.corrected?.has(finding.id) ?? false)}
                    row={row}
                    finding={finding}
                    outcome={item.kind === 'architect' ? architect?.outcome ?? finding?.outcome ?? null : undefined}
                    projectId={projectId}
                    packageId={packageId}
                    onChange={finding && status === 'decided' ? () => setChanging(true) : undefined}
                  />
                )}

                {/* Rows with no result are not items (#1179); these cover an item whose result went away
                    in a re-run while the queue was open. A split page has no countertop card. */}
                {status === 'open' && !finding && split && (
                  <p data-slot="split-no-result" className="rounded-lg border border-dashed p-3 text-sm">
                    No result for this page on the last run, so there is nothing to decide here. Its reason is on Results.
                  </p>
                )}
                {status === 'open' && !finding && row && !question && !split && (
                  <div className="flex flex-col items-start gap-2 rounded-lg border border-dashed p-3 text-sm">
                    <p>{NO_RESULT_HINT}. Its countertop card says why; fill in what it needs there, then run the checks.</p>
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
                  <Button type="submit" disabled={!draft.ready || draft.saving || signedOff}>
                    {draft.saving ? 'Saving…' : 'Record decision'}
                    {!touch && <kbd className="num ml-1 hidden text-xs opacity-70 sm:inline">Enter</kbd>}
                  </Button>
                  {changing && (
                    <Button type="button" variant="ghost" onClick={() => { draft.reset(); setChanging(false); }}>
                      Keep the decision
                    </Button>
                  )}
                  {!touch && <span className="ml-auto hidden text-xs text-muted-foreground sm:inline">1 2 3 choose · N note · J K move</span>}
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
      {/* Which walls were used and who decided (#1202), with GV's field-cut rule behind the "i". */}
      <WallsLine row={row} />
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
  const [savingNow, setSaving] = useState(false);
  // Nothing is answered on a signed-off review (#1182): the buttons stay off, as while saving.
  const signedOff = useSignedOff();
  const saving = savingNow || signedOff;
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
    <section aria-labelledby="queue-walls" data-slot="queue-walls" className="@container flex flex-col gap-2 rounded-lg border p-3">
      <h3 id="queue-walls" className="flex items-center gap-1.5 text-sm font-medium">
        Walls for this countertop
        <WallRuleInfo fieldCut={question.fieldCut} cases={question.cases} />
      </h3>
      {question.betweenPanels && (
        <>
          {/* The server's own reason says it; a sentence of ours only when it does not. */}
          {!question.reason && <p className="text-sm text-muted-foreground">The stone sits between side panels, so the panels take the field cut.</p>}
          <Button size="sm" className="self-start" disabled={saving} onClick={() => void save('back_only')}>
            {BETWEEN_PANELS_WORDS}
          </Button>
          <p className="text-xs text-muted-foreground">Or choose another layout:</p>
        </>
      )}
      {!question.betweenPanels && question.automatic && (
        <p className="text-sm text-muted-foreground">Walls at both ends: seen on the drawing at both ends and confirmed by both AIs. Choose another layout only if that is wrong.</p>
      )}
      {!question.betweenPanels && !question.automatic && question.proposal && (
        <p className="text-sm text-muted-foreground">The readers propose: {wallWords(question.proposal).toLowerCase()}. Not confirmed.</p>
      )}
      {/* One per row, or all of them in one row when three or fewer fit; never a ragged 2 + 1 (#1155). */}
      <ToggleGroup
        type="single"
        variant="outline"
        size="sm"
        spacing={1}
        value={choice}
        onValueChange={setChoice}
        aria-label="Wall layout"
        data-layout={question.choices.length <= 3 ? 'row-when-room' : 'column'}
        className={cn('grid w-full grid-cols-1', question.choices.length <= 3 && '@min-[42rem]:grid-flow-col @min-[42rem]:auto-cols-fr @min-[42rem]:grid-cols-none')}
      >
        {question.choices.map((config) => (
          <ToggleGroupItem key={config} value={config} className="h-auto min-h-8 w-full flex-wrap justify-start gap-x-1.5 gap-y-0.5 px-2.5 py-1">
            <WallLayoutPicture config={config} />
            {wallWords(config)}
            {/* What this choice gives, as the check recorded it (#1202); choosing is still only a draft. */}
            <WallChoiceResult cases={question.cases} layout={config} />
          </ToggleGroupItem>
        ))}
      </ToggleGroup>
      {question.cases?.unavailable_reason && (
        <p data-slot="wall-choices-no-results" className="text-xs text-muted-foreground">{question.cases.unavailable_reason}</p>
      )}
      <div className="flex flex-wrap items-center gap-2">
        <Button size="sm" variant={question.betweenPanels ? 'outline' : 'default'} disabled={!choice || saving} onClick={() => void save(choice)}>
          Use this wall layout
        </Button>
        <Button size="sm" variant="ghost" onClick={onOpenCard}>
          Open countertop card
        </Button>
      </div>
      {question.reason && <p className="text-xs text-muted-foreground">{sentence(question.reason)}</p>}
      <p className="text-xs text-muted-foreground">{signedOff ? SIGNED_OFF_NOTE : 'The result changes only after the checks run again.'}</p>
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
  viewPicked = false,
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
  /** An architect view chosen after the result (#1168). */
  viewPicked?: boolean;
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
            : viewPicked
              ? 'View chosen. It counts once the checks run again.'
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

/**
 * The queue with nothing left open. Empty because the checks have not run on some countertops
 * (#1179), it says that and offers the header's next action (Run checks), never "Nothing needs you".
 */
function DoneState({ empty, notChecked, checksHaveRun, waitingForRun, unconfirmedPages = [], onShowPages, next, onAct, onBrowse }: { empty: boolean; notChecked: number; checksHaveRun: boolean; waitingForRun: number; unconfirmedPages?: readonly number[]; onShowPages?: () => void; next: NextAction; onAct: (kind: NextActionKind) => void; onBrowse: () => void }) {
  const unchecked = empty && notChecked > 0;
  // Never "nothing needs you" while sign-off waits for a page's confirmation (#1191).
  const pagesWait = unconfirmedPages.length > 0;
  // Said to match the button below (#1179): "run the checks" only when Run checks is that button.
  const summary = notCheckedSummary(notChecked, notCheckedSituation(checksHaveRun, next.kind === 'run-checks'));
  // No second run while one is queued or running, or after sign-off (the header's own guard too).
  const runBlocked = !['run-checks', 'review', 'sign-off'].includes(next.kind);
  return (
    <div data-slot="queue-done" className="m-auto flex max-w-md flex-col items-center gap-3 p-6 text-center" role="status">
      {!unchecked && !pagesWait && <CheckCircle2 className="size-10 text-outcome-pass-fg" aria-hidden="true" />}
      <p className="text-lg font-semibold">
        {unchecked ? 'Nothing to decide yet' : pagesWait ? 'Pages with no countertop to confirm' : empty ? 'Nothing needs you' : 'All decisions made'}
      </p>
      {unchecked && (
        <p className="text-sm text-muted-foreground" data-slot="queue-not-checked">
          <span className="num">{notChecked}</span> {summary.lead}{summary.rest && ` ${summary.rest}`}
        </p>
      )}
      {/* Sign-off also waits for these pages (#1191); they are answered on Results, not here. */}
      {unconfirmedPages.length > 0 && (
        <div data-slot="queue-no-countertop" className="flex flex-col items-center gap-2 text-sm">
          <p className="inline-flex flex-wrap items-center justify-center gap-1">
            <OutcomeIcon outcome="REVIEW_REQUIRED" size={12} />
            <span>
              Sign-off also waits for {unconfirmedPages.length === 1 ? 'a page' : 'pages'} with no countertop to be confirmed:{' '}
              {unconfirmedPages.map((page, index) => (
                <span key={page}>{index > 0 && ', '}page <span className="num">{page}</span></span>
              ))}
              .
            </span>
          </p>
          {onShowPages && <Button variant="outline" size="sm" onClick={onShowPages}>Show {unconfirmedPages.length === 1 ? 'it' : 'them'}</Button>}
        </div>
      )}
      {waitingForRun > 0 ? (
        <>
          <p className="text-sm text-muted-foreground">
            <span className="num">{waitingForRun}</span> {waitingForRun === 1 ? 'answer needs' : 'answers need'} a new check run before {waitingForRun === 1 ? 'it shows' : 'they show'} in the results. The run replaces the current results; a decision carries over only where its result comes back unchanged.
          </p>
          <Button disabled={runBlocked} onClick={() => onAct('run-checks')}>Run checks</Button>
          {runBlocked && next.reason && <p className="text-xs text-muted-foreground">{next.reason}</p>}
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

/**
 * A page listed with no countertop, as a queue item (#1191): the readers' reason and the two
 * answers, nothing pre-selected, sent only on a click. "There is a countertop" makes the page a
 * countertop that waits for a check run; "Confirm" settles it.
 */
function PageQuestion({ page, status, onAnswer, onAnswered }: { page: PageWithoutCountertop | null; status: ItemStatus; onAnswer?: AnswerNoCountertop; onAnswered: () => void }) {
  const signedOff = useSignedOff();
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (status === 'waiting-for-run') {
    return (
      <p data-slot="queue-page-countertop" className="rounded-lg border border-dashed p-3 text-sm">
        You said this page has a countertop. Run the checks: it then needs your decision like any countertop with no line chosen.
      </p>
    );
  }
  if (!page) return <p className="rounded-lg border border-dashed p-3 text-sm text-muted-foreground">This page is not in the current results.</p>;
  async function answer(choice: 'no_countertop' | 'countertop') {
    if (!onAnswer || !page || saving || signedOff) return;
    setSaving(true);
    setError(null);
    try {
      await onAnswer(page, choice);
      onAnswered();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setSaving(false);
    }
  }
  const off = signedOff || saving || !onAnswer || !page.record_id;
  return (
    <div data-slot="queue-page" className="flex flex-col gap-3">
      <p className="text-sm text-muted-foreground">
        {page.older_reading && <span className="mr-1.5 rounded border px-1 py-px text-xs font-medium text-foreground">Older reading</span>}
        {page.reason}
      </p>
      {page.confirmation ? (
        <p className="inline-flex items-center gap-1 text-sm font-medium text-outcome-pass-fg">
          <CheckCircle2 className="size-4" aria-hidden="true" /> Confirmed: no countertop here · {page.confirmation.confirmed_by}
        </p>
      ) : (
        <div className="flex flex-wrap gap-2">
          <Button type="button" disabled={off} onClick={() => void answer('no_countertop')}>Confirm: no countertop here</Button>
          <Button type="button" variant="outline" disabled={off} title={THERE_IS_A_COUNTERTOP_TIP} onClick={() => void answer('countertop')}>There is a countertop</Button>
        </div>
      )}
      {signedOff && <p role="note" className="text-xs text-muted-foreground">{SIGNED_OFF_NOTE}</p>}
      {error && <p role="alert" className="text-sm text-destructive">Not saved: {error}</p>}
    </div>
  );
}
