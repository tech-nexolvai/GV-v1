import { CheckCircle2, CircleAlert, Loader2, RefreshCw, Signature } from 'lucide-react';

import type { ApprovalReadiness } from '@/api/client';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import type { SignOffSummary } from '@/lib/countertop-results';

/** What is being signed, in counts the page already has. */
export interface SignOffScope {
  /** Countertops by who settled them; null while the countertop results load or when they failed. */
  countertops: SignOffSummary | null;
  /** True when the countertop results could not be loaded (the split is then unavailable). */
  countertopsFailed: boolean;
  /** The countertops' "matches the architect" results (#1085); null until countertops load. */
  architect?: number | null;
  /**
   * Recorded results that are not a countertop's own, the same set the Results page lists under
   * "Other checks" (package-level checks); null until countertops load.
   */
  otherChecks: number | null;
  /** Every recorded result of the revision: the server approves them all. */
  total: number;
  /**
   * Countertops whose architect view a reviewer chose after the last check run (#1168): the server's
   * `waits_for_run`, plus picks made in this sitting. Only a new run uses them; the readiness API
   * blocks sign-off until then, and this says why in words.
   */
  viewPicksWaiting?: number;
}

function results(count: number) {
  return count === 1 ? 'recorded result' : 'recorded results';
}

/** "all 16 recorded results", or "the 1 recorded result". */
function allResults(count: number) {
  return count === 1 ? <>the <span className="num">1</span> recorded result</> : <>all <span className="num">{count}</span> recorded results</>;
}

/** "1 needed no decision · 2 decided by you · 7 not checkable": numbers in the number face only. */
function CountertopLine({ summary }: { summary: SignOffSummary }) {
  const parts: [number, string][] = [
    [summary.byChecks, 'needed no decision'],
    [summary.byYou, 'decided by you'],
    [summary.notCheckable, 'not checkable'],
  ];
  if (summary.needYou > 0) parts.push([summary.needYou, summary.needYou === 1 ? 'still needs you' : 'still need you']);
  // A row with no recorded result is not a finding, so it is not part of what the server approves.
  if (summary.noResult > 0) parts.push([summary.noResult, 'with no recorded result']);
  return (
    <>
      {parts.map(([count, words], index) => (
        <span key={words}>
          {index > 0 && ' · '}
          <span className="num">{count}</span> {words}
        </span>
      ))}
    </>
  );
}

/**
 * The Sign off step (#1064), shown when the review stepper reaches it: whether the server says the
 * review is ready, what the sign-off covers, and the button that opens the confirmation. Readiness is
 * the server's answer (`approval-readiness`); nothing here decides it.
 */
export function SignOffPanel({
  readiness,
  ready,
  scope,
  busy,
  onSignOff,
  onReview,
  onRunChecks,
}: {
  readiness: ApprovalReadiness | null;
  /** The page's own `canSignOff` (readiness says yes, nothing blocks, no run is pending). */
  ready: boolean;
  scope: SignOffScope;
  busy: boolean;
  onSignOff: () => void;
  onReview: () => void;
  /** Takes the reviewer to Run checks (the Measurements form saves first). */
  onRunChecks?: () => void;
}) {
  const blocking = readiness?.blocking_findings ?? 0;
  const picks = scope.viewPicksWaiting ?? 0;
  return (
    <section
      data-tw
      data-slot="signoff-panel"
      aria-labelledby="signoff-panel-title"
      className="mx-4 mt-4 flex flex-col gap-3 rounded-xl border bg-card p-4 font-sans text-card-foreground sm:mx-6"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 id="signoff-panel-title" className="flex items-center gap-2 text-base font-semibold">
          <Signature className="size-4" aria-hidden="true" /> Sign off
        </h2>
        {readiness === null ? (
          <span className="inline-flex items-center gap-1 text-sm text-muted-foreground">
            <Loader2 className="size-3.5 animate-spin" aria-hidden="true" /> Checking…
          </span>
        ) : ready ? (
          <span className="inline-flex items-center gap-1 rounded-full border border-outcome-pass-fg/30 bg-outcome-pass-bg px-2.5 py-0.5 text-sm font-medium text-outcome-pass-fg" data-part="ready">
            <CheckCircle2 className="size-3.5" aria-hidden="true" /> Ready
          </span>
        ) : (
          <span className="inline-flex items-center gap-1 rounded-full border border-outcome-review-fg/60 bg-outcome-review-bg px-2.5 py-0.5 text-sm font-medium text-outcome-review-fg" data-part="not-ready">
            <CircleAlert className="size-3.5" aria-hidden="true" /> Not ready
          </span>
        )}
      </div>

      {/* A view chosen after the last run (#1168): said whatever readiness says, never enabling anything. */}
      {picks > 0 && (
        <div data-part="view-picks-waiting" className="flex flex-wrap items-center gap-2 text-sm" role="status">
          <span className="inline-flex items-center gap-1 font-medium">
            <RefreshCw className="size-3.5" aria-hidden="true" /> Run the checks again before signing off.
          </span>
          <span className="text-muted-foreground">
            A view was chosen for <span className="num">{picks}</span> {picks === 1 ? 'countertop' : 'countertops'} after the last check run.
          </span>
          {onRunChecks && <Button type="button" size="sm" variant="outline" onClick={onRunChecks}>Run checks</Button>}
        </div>
      )}

      {readiness !== null && !ready && (
        <div className="flex flex-wrap items-center gap-2 text-sm" role="status">
          {blocking > 0 && (
            <span className="font-medium"><span className="num">{blocking}</span> {blocking === 1 ? 'result still needs' : 'results still need'} a decision.</span>
          )}
          {readiness.reason && <span className="text-muted-foreground">{readiness.reason}</span>}
          {blocking > 0 && (
            <Button type="button" size="sm" variant="outline" onClick={onReview}>Review them</Button>
          )}
        </div>
      )}

      <dl className="grid gap-x-4 gap-y-1 text-sm sm:grid-cols-[auto_1fr]">
        <dt className="text-muted-foreground">Countertops</dt>
        <dd data-part="countertops">
          {scope.countertops ? <CountertopLine summary={scope.countertops} /> : scope.countertopsFailed ? 'Not available: the countertop results did not load.' : 'Loading…'}
        </dd>
        {scope.architect !== undefined && scope.architect !== null && scope.architect > 0 && (
          <>
            <dt className="text-muted-foreground">Matches the architect</dt>
            <dd data-part="architect-checks"><span className="num">{scope.architect}</span> {results(scope.architect)}</dd>
          </>
        )}
        <dt className="text-muted-foreground">Other checks</dt>
        <dd data-part="other-checks">
          {scope.otherChecks !== null
            ? <><span className="num">{scope.otherChecks}</span> {results(scope.otherChecks)}</>
            : scope.countertopsFailed ? 'Not available' : 'Loading…'}
        </dd>
      </dl>
      <p className="text-sm text-muted-foreground">
        Signing off approves {allResults(scope.total)} of this revision. It cannot be undone.
      </p>

      <div>
        <Button type="button" onClick={onSignOff} disabled={!ready || busy}>
          {busy ? <><Loader2 className="animate-spin" aria-hidden="true" /> Signing off…</> : 'Sign off…'}
        </Button>
      </div>
    </section>
  );
}

/**
 * The confirmation before a sign-off (#1064). Sign-off cannot be undone (an approved revision only
 * leaves that state by being replaced by a new upload), so it names who is signing and what.
 *
 * The signer is the reviewer on the server's own record of this person's review sittings; the API
 * records the signed-in caller either way, so when no sitting names them the dialog says that.
 */
export function SignOffDialog({
  open,
  onOpenChange,
  signer,
  vendor,
  revision,
  scope,
  ready,
  busy,
  error,
  onConfirm,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  signer: string | null;
  vendor: string;
  revision: number | null;
  scope: SignOffScope;
  /** The server still says the review can be signed; confirm is disabled otherwise. */
  ready: boolean;
  busy: boolean;
  /** Why the last attempt did not sign, shown here so it is not hidden behind the dialog. */
  error: string | null;
  onConfirm: () => void;
}) {
  return (
    <Dialog open={open} onOpenChange={(next) => { if (!busy) onOpenChange(next); }}>
      <DialogContent data-slot="signoff-dialog" className="font-sans sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Sign off this review?</DialogTitle>
          <DialogDescription>
            {signer ? (
              <>You are signing as <strong className="text-foreground" data-part="signer">{signer}</strong>.</>
            ) : (
              <>The sign-off is recorded under your sign-in.</>
            )}
          </DialogDescription>
        </DialogHeader>
        <ul className="flex list-disc flex-col gap-1.5 pl-5 text-sm">
          <li>
            {vendor}
            {revision !== null && <>, revision <span className="num">{revision}</span></>}:{' '}
            {allResults(scope.total)} {scope.total === 1 ? 'is' : 'are'} approved.
          </li>
          {scope.countertops && (
            <li>
              Countertops: <CountertopLine summary={scope.countertops} />.
            </li>
          )}
          <li>The signed files are prepared next: findings PDF, workbook and drawing redline.</li>
          <li><strong>This cannot be undone.</strong> Changes after sign-off need a new revision of the drawings.</li>
        </ul>
        {error !== null && (
          <p className="text-sm text-destructive" role="alert" data-part="signoff-error">Nothing was signed: {error}</p>
        )}
        <DialogFooter>
          <DialogClose asChild>
            <Button type="button" variant="outline" disabled={busy}>Keep reviewing</Button>
          </DialogClose>
          <Button type="button" onClick={onConfirm} disabled={busy || !ready}>
            {busy ? <><Loader2 className="animate-spin" aria-hidden="true" /> Signing off…</> : 'Sign off'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
