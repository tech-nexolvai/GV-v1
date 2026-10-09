import { CheckCircle2, CircleAlert, Loader2, Signature } from 'lucide-react';

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

/** What is being signed, in counts the page already has. Null parts are still loading. */
export interface SignOffScope {
  /** Countertops by who settled them; null while the countertop results load or if they failed. */
  countertops: SignOffSummary | null;
  /** Recorded results that are not a countertop's (package-level checks); null while loading. */
  packageChecks: number | null;
  /** Every recorded result of the revision: the server approves them all. */
  total: number;
}

/** "1 needed no decision · 2 decided by you · 7 not checkable": numbers in the number face only. */
function CountertopLine({ summary }: { summary: SignOffSummary }) {
  const parts: [number, string][] = [
    [summary.byChecks, 'needed no decision'],
    [summary.byYou, 'decided by you'],
    [summary.notCheckable, 'not checkable'],
  ];
  if (summary.needYou > 0) parts.push([summary.needYou, summary.needYou === 1 ? 'still needs you' : 'still need you']);
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
}: {
  readiness: ApprovalReadiness | null;
  /** The page's own `canSignOff` (readiness says yes, nothing blocks, no run is pending). */
  ready: boolean;
  scope: SignOffScope;
  busy: boolean;
  onSignOff: () => void;
  onReview: () => void;
}) {
  const blocking = readiness?.blocking_findings ?? 0;
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
          {scope.countertops ? <CountertopLine summary={scope.countertops} /> : 'Loading…'}
        </dd>
        <dt className="text-muted-foreground">Package checks</dt>
        <dd data-part="package-checks">
          {scope.packageChecks === null ? 'Loading…' : <><span className="num">{scope.packageChecks}</span> {scope.packageChecks === 1 ? 'recorded result' : 'recorded results'}</>}
        </dd>
      </dl>
      <p className="text-sm text-muted-foreground">
        Signing off approves all <span className="num">{scope.total}</span> recorded results of this revision. It cannot be undone.
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
  busy,
  onConfirm,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  signer: string | null;
  vendor: string;
  revision: number | null;
  scope: SignOffScope;
  busy: boolean;
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
            all <span className="num">{scope.total}</span> recorded results are approved.
          </li>
          {scope.countertops && (
            <li>
              Countertops: <CountertopLine summary={scope.countertops} />.
            </li>
          )}
          <li>The signed files are prepared next: findings PDF, workbook and drawing redline.</li>
          <li><strong>This cannot be undone.</strong> Changes after sign-off need a new revision of the drawings.</li>
        </ul>
        <DialogFooter>
          <DialogClose asChild>
            <Button type="button" variant="outline" disabled={busy}>Keep reviewing</Button>
          </DialogClose>
          <Button type="button" onClick={onConfirm} disabled={busy}>
            {busy ? <><Loader2 className="animate-spin" aria-hidden="true" /> Signing off…</> : 'Sign off'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
