/**
 * Where a review stands, and the one thing to do next (#1034).
 *
 * A pure function of facts the API already records — package state, the live run's finding count,
 * the approval-readiness answer and the signed-export status — plus two things only this browser
 * session knows (checks were just queued; values were saved after the last run). The stepper, the
 * header's primary button and the Documents cards all read it, so they can never disagree.
 *
 * **It never invents.** An unknown fact stays unknown: a readiness answer that has not loaded gives
 * no count rather than 0, and nothing is "done" because a fact is missing. It decides nothing about
 * the drawing either — it only says which step of the workflow the recorded facts put the review in.
 */

import { costText } from '@/lib/usage';

export type StepId = 'upload' | 'reading' | 'checks' | 'decisions' | 'signoff' | 'report';
export type StepState = 'done' | 'current' | 'blocked' | 'upcoming';

export interface ReviewStep {
  id: StepId;
  label: string;
  state: StepState;
  /** A number worth showing on the step (checks recorded, items needing you); null when unknown. */
  count: number | null;
  /** One short line: why the step is blocked, or what it is doing. */
  note: string | null;
}

export type NextActionKind =
  | 'wait-upload'
  | 'wait-reading'
  | 'run-checks'
  | 'checking'
  | 'review'
  | 'sign-off'
  | 'preparing-report'
  | 'prepare-report'
  | 'download-report'
  | 'none';

export interface NextAction {
  kind: NextActionKind;
  /** The button's words; the same on the review header and the Documents card. */
  label: string;
  /** True when the action is a wait, or when the server says it cannot happen yet. */
  disabled: boolean;
  /** Shown beside a disabled action so the reviewer is never left guessing why. */
  reason: string | null;
}

/**
 * Where the reading is while it runs (#1178), as `GET …/packages/{id}` reports it (`reading`).
 * Every field is recorded by the worker; none is estimated here.
 */
export interface ReadingProgress {
  /** The part of the reading that is running, in plain words. */
  label: string;
  /** Pages this part has finished; null where it does not go page by page. */
  pagesDone: number | null;
  /** Pages this part works on; null where that is not known. */
  pagesTotal: number | null;
  /** Model calls the reading has made so far. */
  calls: number;
  /** What those calls cost so far, in US dollars (exact decimal text from the API). */
  costUsd: string;
  /** Calls with no published price, left out of `costUsd`. */
  unpricedCalls: number;
  /** When the worker last wrote a progress line (ISO time); null before its first. */
  updatedAt?: string | null;
  /** A failed reading is being tried again (the package still says FAILED_RETRYABLE). */
  retrying?: boolean;
}

/**
 * How long without a new progress line before the screen says so (#1178 review). One reader call may
 * take up to three minutes, so a quieter stretch than that is normal; five minutes is not.
 */
export const STALE_PROGRESS_MINUTES = 5;

export interface ReviewFacts {
  /** The package's recorded lifecycle state (`PackageOut.state`). */
  state: string;
  /** `required-inputs.still_reading`, when this screen has asked; null when it has not. */
  stillReading?: boolean | null;
  /** `required-inputs.revision_state`, the pipeline's own stage name while reading. */
  readingStage?: string | null;
  /** The worker's own progress line while reading (#1178); preferred over `readingStage`. */
  reading?: ReadingProgress | null;
  /** The time now, in ms, for "no progress for N minutes"; leave out to never say it. */
  nowMs?: number;
  /** Findings recorded by the live check run; null when not loaded. */
  findingsTotal: number | null;
  /** Checks were queued from this screen and their results have not arrived yet. */
  checksQueued?: boolean;
  /** Values were saved in this session after the last check run (the API records no time for values). */
  valuesChangedSinceRun?: boolean;
  /** The approval-readiness answer; null when not loaded or unavailable. */
  readiness: { blockingFindings: number; canApprove: boolean; reason: string | null } | null;
  /** Signed-export status after approval; null when not loaded or not applicable. */
  exports: 'not_requested' | 'preparing' | 'ready' | 'failed' | null;
  /**
   * Why the reading stopped, in words a person can act on (`PackageOut.stop_reason`, #1206): e.g.
   * the AI provider account is out of credit. Shown as it is; null when the API gives none.
   */
  stopReason?: string | null;
}

export interface ReviewStage {
  steps: ReviewStep[];
  /** The first step that is not done; null when everything is done. */
  current: StepId | null;
  next: NextAction;
}

const LABELS: Record<StepId, string> = {
  upload: 'Upload',
  reading: 'Reading',
  checks: 'Checks',
  decisions: 'Decisions',
  signoff: 'Sign off',
  report: 'Report',
};

const UPLOADING = new Set(['CREATED', 'UPLOADING']);
const READING = new Set(['UPLOADED', 'INGESTING', 'EXTRACTING', 'MATCHING', 'VALIDATING_EVIDENCE']);
const CHECKING = new Set(['RUNNING_CHECKS', 'GENERATING_OUTPUTS']);
const HISTORICAL = new Set(['CANCELLED', 'SUPERSEDED']);

export function reviewStage(facts: ReviewFacts): ReviewStage {
  const { state, readiness } = facts;
  const approved = state === 'APPROVED';
  const failedForGood = state === 'FAILED_PERMANENT';
  const retrying = state === 'FAILED_RETRYABLE';
  // A stop a person must act on (#1206): it waits for them, never retries on its own.
  const stoppedFor = (failedForGood || retrying) && facts.stopReason ? facts.stopReason : null;

  const uploadDone = !UPLOADING.has(state);
  const retryingReading = retrying && facts.reading?.retrying === true;
  const reading = READING.has(state) || facts.stillReading === true || retryingReading;
  const readingDone = uploadDone && !reading;
  const checking = CHECKING.has(state) || facts.checksQueued === true;
  const hasRun = (facts.findingsTotal ?? 0) > 0;
  const checksDone = approved || (readingDone && !checking && hasRun && !facts.valuesChangedSinceRun);
  const blocking = readiness ? readiness.blockingFindings : null;
  const decisionsDone = approved || (checksDone && blocking === 0);
  const signoffDone = approved;
  const reportDone = approved && facts.exports === 'ready';

  const done: Record<StepId, boolean> = {
    upload: uploadDone,
    reading: readingDone,
    checks: checksDone,
    decisions: decisionsDone,
    signoff: signoffDone,
    report: reportDone,
  };
  const order: StepId[] = ['upload', 'reading', 'checks', 'decisions', 'signoff', 'report'];
  const current = order.find((id) => !done[id]) ?? null;

  const steps: ReviewStep[] = order.map((id) => {
    const step: ReviewStep = { id, label: LABELS[id], state: done[id] ? 'done' : id === current ? 'current' : 'upcoming', count: null, note: null };
    if (id === 'checks') step.count = facts.findingsTotal !== null && facts.findingsTotal > 0 ? facts.findingsTotal : null;
    if (id === 'decisions' && checksDone && !approved) step.count = blocking;
    if (id === current) {
      if (id === 'reading') step.note = readingNote(facts);
      if (id === 'checks' && checking) step.note = 'Running';
      if (id === 'checks' && !checking && hasRun && facts.valuesChangedSinceRun) step.note = 'Values changed since the last run';
      if (id === 'signoff' && readiness && !readiness.canApprove) {
        step.state = 'blocked';
        step.note = readiness.reason ?? 'The server says sign-off is not possible yet';
      }
      if (id === 'report' && facts.exports === 'failed') {
        step.state = 'blocked';
        step.note = 'The signed files could not be prepared';
      }
      if (failedForGood) {
        step.state = 'blocked';
        step.note = 'Processing failed';
      }
      // While the retry is visibly reading, the Reading step says so instead (#1178 review).
      if (retrying && !retryingReading) step.note = 'Processing hit a problem and will retry';
      if (stoppedFor && !retryingReading) {
        step.state = 'blocked';
        step.note = stoppedFor;
      }
    }
    return step;
  });

  return { steps, current, next: nextAction(facts, current, { checking, blocking }) };
}

function nextAction(
  facts: ReviewFacts,
  current: StepId | null,
  { checking, blocking }: { checking: boolean; blocking: number | null },
): NextAction {
  if (HISTORICAL.has(facts.state)) {
    return { kind: 'none', label: facts.state === 'SUPERSEDED' ? 'Replaced by a newer upload' : 'Cancelled', disabled: true, reason: 'This is a historical record.' };
  }
  if ((facts.state === 'FAILED_PERMANENT' || facts.state === 'FAILED_RETRYABLE') && facts.stopReason && facts.reading?.retrying !== true) {
    return { kind: 'none', label: 'Reading stopped', disabled: true, reason: facts.stopReason };
  }
  if (facts.state === 'FAILED_PERMANENT') {
    return { kind: 'none', label: 'Processing failed', disabled: true, reason: 'Processing stopped and will not retry on its own. Missing results do not mean the drawing passed.' };
  }
  switch (current) {
    case 'upload':
      return { kind: 'wait-upload', label: 'Waiting for upload', disabled: true, reason: 'The drawings have not finished uploading.' };
    case 'reading':
      return { kind: 'wait-reading', label: 'Reading…', disabled: true, reason: readingReason(facts) };
    case 'checks':
      return checking
        ? { kind: 'checking', label: 'Checking…', disabled: true, reason: 'Checks are running. Results refresh when they finish.' }
        : { kind: 'run-checks', label: 'Run checks', disabled: false, reason: null };
    case 'decisions':
      return blocking === null
        ? { kind: 'review', label: 'Review results', disabled: false, reason: null }
        : { kind: 'review', label: `Review ${blocking} ${blocking === 1 ? 'item' : 'items'}`, disabled: false, reason: null };
    case 'signoff':
      return facts.readiness?.canApprove
        ? { kind: 'sign-off', label: 'Sign off', disabled: false, reason: null }
        : { kind: 'sign-off', label: 'Sign off', disabled: true, reason: facts.readiness?.reason ?? 'Checking whether sign-off is possible…' };
    case 'report':
      if (facts.exports === 'preparing') return { kind: 'preparing-report', label: 'Preparing report…', disabled: true, reason: 'The signed files are being prepared.' };
      // A failed export is not retried by asking again: the server answers with the same failed
      // request (#1064). So no button promises a retry; an admin retries the export.
      if (facts.exports === 'failed') return { kind: 'prepare-report', label: 'Report failed', disabled: true, reason: 'The signed files could not be prepared, and nothing was published. An admin needs to retry the export.' };
      if (facts.exports === 'not_requested') return { kind: 'prepare-report', label: 'Prepare report', disabled: false, reason: null };
      return { kind: 'preparing-report', label: 'Report', disabled: true, reason: 'Checking the signed files…' };
    case null:
      return { kind: 'download-report', label: 'Download report', disabled: false, reason: null };
  }
}

/**
 * "page k of N" for a part that goes page by page: k is the page being worked on (one past those
 * done), so the last page reads "page N of N"; with the count unknown, only "N pages"; else nothing.
 */
export function pageWords(reading: Pick<ReadingProgress, 'pagesDone' | 'pagesTotal'>): string | null {
  const { pagesDone, pagesTotal } = reading;
  if (pagesTotal === null || pagesTotal <= 0) return null;
  if (pagesDone === null) return `${pagesTotal} ${pagesTotal === 1 ? 'page' : 'pages'}`;
  return `page ${Math.min(pagesDone + 1, pagesTotal)} of ${pagesTotal}`;
}

/** The Reading step's line: the part that is running and, where known, the page. */
function readingNote(facts: ReviewFacts): string | null {
  const reading = facts.reading;
  if (reading) {
    return [reading.retrying ? 'Retrying' : null, reading.label, pageWords(reading), quietWords(reading, facts.nowMs)]
      .filter(Boolean)
      .join(' · ');
  }
  return facts.readingStage ? stageWords(facts.readingStage) : null;
}

/** "no progress for N minutes" once the worker has been quiet too long (e.g. it was stopped); else null. */
export function quietWords(reading: Pick<ReadingProgress, 'updatedAt'>, nowMs: number | undefined): string | null {
  if (nowMs === undefined || !reading.updatedAt) return null;
  const last = Date.parse(reading.updatedAt);
  if (!Number.isFinite(last)) return null;
  const minutes = Math.floor((nowMs - last) / 60_000);
  if (minutes < STALE_PROGRESS_MINUTES) return null;
  return `no progress for ${minutes} minutes`;
}

/** The header's reason while reading: the same line, plus the calls and cost so far. */
function readingReason(facts: ReviewFacts): string {
  const note = readingNote(facts);
  if (!note) return 'The AI is reading the drawings.';
  const reading = facts.reading;
  if (!reading || reading.calls === 0) return `AI reading: ${note}.`;
  const spent = costText(reading.costUsd, reading.calls, reading.unpricedCalls);
  return `AI reading: ${note}. ${reading.calls} AI ${reading.calls === 1 ? 'call' : 'calls'} so far, ${spent}.`;
}

/** The pipeline's own stage name, in lower-case words ("VALIDATING_EVIDENCE" → "validating evidence"). */
function stageWords(stage: string): string {
  return stage.toLowerCase().replace(/_/g, ' ');
}
