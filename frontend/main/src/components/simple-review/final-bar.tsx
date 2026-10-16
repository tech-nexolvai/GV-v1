import { useEffect, useRef, useState } from 'react';
import { CheckCheck, CircleAlert, Download, FileCheck2, Loader2, Lock, RotateCcw } from 'lucide-react';

import type { FinalCloudApi, SignOffFacts, SignedFilesStatus } from '@/api/final-cloud';
import type { ReviewSummary } from '@/lib/review-items';
import { Button } from '@/components/ui/button';
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';

/** How often the bar asks whether the signed files are ready, while they are being made. */
export const FINAL_POLL_MS = 3000;

type Phase =
  | { kind: 'unsigned' }
  | { kind: 'checking' }
  | { kind: 'signed'; files: SignedFilesStatus }
  | { kind: 'error'; message: string; signed: boolean; retry: Retry };

/** What the error's button does: read where the files are again, or ask for the files again. */
type Retry = 'status' | 'prepare';

function words(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/**
 * Whether the server's own reason asks for a check run (`INPUTS_CHANGED_NEEDS_RERUN`, "You changed
 * inputs after the last check run. Run the checks before signing off."): no count on the page
 * explains it, so the bar says it and offers the run.
 */
function asksForCheckRun(reason: string | null): boolean {
  return reason !== null && /\brun the checks\b/i.test(reason);
}

/** The server's words as a sentence, ending with one full stop. */
function sentence(text: string): string {
  const trimmed = text.trim();
  return /[.!?]$/.test(trimmed) ? trimmed : `${trimmed}.`;
}

/**
 * The bottom of the page (#1368): the final cloud PDF. It says what is left in plain words, with
 * the counts of the header the page prints (#1394); the server's `blocked_reason` only when no
 * count explains it. The button is
 * unlocked only by the server's `can_finalise`; it opens the existing sign-off, then the signed
 * files are made and the cloud PDF can be downloaded here.
 */
export function FinalBar({
  summary,
  left,
  api,
  projectId,
  packageId,
  revision,
  approved,
  onSigned,
  onRechecked,
}: {
  summary: ReviewSummary;
  /** What is left, in words ("9 items need you · 54 not checked"), from the page's W1 header. */
  left: readonly string[];
  api: FinalCloudApi;
  projectId: string | null;
  packageId: string;
  revision: number | null;
  /** The package was already signed off when the page loaded (its state is APPROVED). */
  approved: boolean;
  /** After a sign-off: the page reloads its lists (every answer is now closed). */
  onSigned: () => void;
  /** After "Run the checks again" was recorded: the page reloads its lists. */
  onRechecked?: () => void;
}) {
  const [signedNow, setSignedNow] = useState(false);
  const [files, setFiles] = useState<SignedFilesStatus | null>(null);
  const [statusAsk, setStatusAsk] = useState(0);
  const [failure, setFailure] = useState<{ message: string; retry: Retry } | null>(null);
  // The signed files are being asked for: where they are is not read meanwhile, so an older answer
  // never overwrites the request's own.
  const [making, setMaking] = useState(false);
  const [dialog, setDialog] = useState(false);
  const [facts, setFacts] = useState<SignOffFacts | null | 'loading'>(null);
  const [stillApply, setStillApply] = useState(false);
  const [signing, setSigning] = useState(false);
  const [signError, setSignError] = useState<string | null>(null);
  const [downloading, setDownloading] = useState(false);
  const [asking, setAsking] = useState(false);
  const [askError, setAskError] = useState<string | null>(null);
  const mounted = useRef(true);
  useEffect(() => {
    // Set again on every mount: StrictMode (the dev server) mounts, unmounts and mounts again (#1394).
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const signed = approved || signedNow;

  // Once signed off (before the page opened, or here): where the signed files are.
  useEffect(() => {
    if (!signed || projectId === null || failure !== null || making) return;
    let current = true;
    api.status(projectId, packageId).then(
      (answer) => {
        if (!current) return;
        setFiles(answer);
        setFailure(null);
      },
      (error: unknown) => current && setFailure({ message: `Where the final cloud PDF is could not be read: ${words(error)}`, retry: 'status' }),
    );
    return () => {
      current = false;
    };
  }, [api, signed, projectId, packageId, statusAsk, failure, making]);

  // While the files are being made, ask again every few seconds.
  useEffect(() => {
    if (files !== 'preparing') return;
    const timer = window.setTimeout(() => setStatusAsk((n) => n + 1), FINAL_POLL_MS);
    return () => window.clearTimeout(timer);
  }, [files, statusAsk]);

  const phase: Phase = failure !== null
    ? { kind: 'error', message: failure.message, signed, retry: failure.retry }
    : !signed
      ? { kind: 'unsigned' }
      : files === null
        ? { kind: 'checking' }
        : { kind: 'signed', files };

  function openDialog() {
    if (projectId === null || !summary.canFinalise) return;
    setSignError(null);
    setStillApply(false);
    setDialog(true);
    setFacts('loading');
    api.facts(projectId, packageId).then(
      (answer) => mounted.current && setFacts(answer),
      () => mounted.current && setFacts(null),
    );
  }

  async function signOff() {
    if (projectId === null || signing) return;
    const carried = facts !== null && facts !== 'loading' && facts.carried.length > 0;
    setSigning(true);
    setSignError(null);
    try {
      await api.signOff(projectId, packageId, carried && stillApply);
    } catch (error) {
      // Refused or failed before it was recorded: nothing was signed, and the dialog says so.
      if (mounted.current) {
        setSignError(words(error));
        setSigning(false);
      }
      return;
    }
    // The sign-off is recorded. From here on nothing may say it was not: a failure to make the PDF
    // is the signed-off error, whose Try again only asks for the PDF.
    setSigning(false);
    setDialog(false);
    setSignedNow(true);
    onSigned();
    await prepare();
  }

  /** Ask for the signed files of the recorded sign-off. Never signs again. */
  async function prepare() {
    if (projectId === null || making) return;
    setMaking(true);
    setFailure(null);
    setFiles('preparing');
    try {
      const answer = await api.prepare(projectId, packageId);
      if (mounted.current) setFiles(answer);
    } catch (error) {
      if (mounted.current) {
        setFailure({ message: `Sign-off was recorded, but the final cloud PDF could not be made: ${sentence(words(error))} Try again.`, retry: 'prepare' });
      }
    } finally {
      if (mounted.current) setMaking(false);
    }
  }

  /** Ask for the checks to run again (the existing `POST …/checks`). */
  async function runChecks() {
    if (projectId === null || asking || !api.runChecks) return;
    setAsking(true);
    setAskError(null);
    try {
      await api.runChecks(projectId, packageId);
      if (mounted.current) onRechecked?.();
    } catch (error) {
      if (mounted.current) setAskError(`The checks could not be asked for: ${sentence(words(error))}`);
    } finally {
      if (mounted.current) setAsking(false);
    }
  }

  async function download() {
    if (projectId === null || downloading) return;
    setDownloading(true);
    try {
      const blob = await api.download(projectId, packageId);
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = `gv-review-${packageId}${revision !== null ? `-r${revision}` : ''}-cloud.pdf`;
      link.click();
      URL.revokeObjectURL(url);
    } catch (error) {
      setFailure({ message: `The final cloud PDF could not be downloaded: ${words(error)}`, retry: 'status' });
    } finally {
      if (mounted.current) setDownloading(false);
    }
  }

  // ── What the bar says ──
  let icon: React.ReactNode;
  let headline: React.ReactNode;
  let sub: React.ReactNode = null;
  let action: React.ReactNode;
  const locked = phase.kind === 'unsigned' && !summary.canFinalise;

  if (phase.kind === 'error') {
    icon = <CircleAlert aria-hidden="true" className="size-4 text-outcome-fail-fg" />;
    headline = <span className="text-outcome-fail-fg">{phase.signed ? 'Signed off, but something went wrong' : 'Something went wrong'}</span>;
    sub = phase.message;
    action = phase.retry === 'prepare' ? (
      <Button variant="outline" onClick={() => void prepare()} disabled={making}>
        <RotateCcw aria-hidden="true" />
        Try again
      </Button>
    ) : (
      <Button
        variant="outline"
        onClick={() => {
          setFailure(null);
          setStatusAsk((n) => n + 1);
        }}
      >
        <RotateCcw aria-hidden="true" />
        Retry
      </Button>
    );
  } else if (phase.kind === 'checking') {
    icon = <Loader2 aria-hidden="true" className="size-4 animate-spin text-muted-foreground motion-reduce:animate-none" />;
    headline = 'Signed off. Looking for the final cloud PDF…';
    action = null;
  } else if (phase.kind === 'signed') {
    if (phase.files === 'ready') {
      icon = <FileCheck2 aria-hidden="true" className="size-4 text-foreground" />;
      headline = 'Signed off. The final cloud PDF is ready.';
      sub = 'It marks every item as it was left: confirmed, fixed, wrong and not checked.';
      action = (
        <Button onClick={() => void download()} disabled={downloading}>
          {downloading ? <Loader2 aria-hidden="true" className="animate-spin motion-reduce:animate-none" /> : <Download aria-hidden="true" />}
          Download final cloud PDF
        </Button>
      );
    } else if (phase.files === 'preparing') {
      icon = <Loader2 aria-hidden="true" className="size-4 animate-spin text-muted-foreground motion-reduce:animate-none" />;
      headline = 'Signed off. Making the final cloud PDF…';
      sub = 'This takes a minute. You can stay on this page; it updates by itself.';
      action = (
        <Button disabled>
          <Loader2 aria-hidden="true" className="animate-spin motion-reduce:animate-none" />
          Making the PDF…
        </Button>
      );
    } else {
      const failed = phase.files === 'failed';
      icon = failed ? <CircleAlert aria-hidden="true" className="size-4 text-outcome-fail-fg" /> : <CheckCheck aria-hidden="true" className="size-4 text-muted-foreground" />;
      headline = failed ? <span className="text-outcome-fail-fg">Failed: the final cloud PDF could not be made.</span> : 'Signed off. The final cloud PDF has not been made yet.';
      sub = failed ? 'Nothing is signed again: the same sign-off is used.' : null;
      action = (
        <Button onClick={() => void prepare()} disabled={making}>
          <FileCheck2 aria-hidden="true" />
          {failed ? 'Try again' : 'Create final cloud PDF'}
        </Button>
      );
    }
  } else if (summary.canFinalise) {
    icon = <CheckCheck aria-hidden="true" className="size-4 text-foreground" />;
    headline = 'Everything is checked.';
    sub = 'Creating the final cloud PDF signs off this revision first. It cannot be undone.';
    action = (
      <Button onClick={openDialog}>
        <FileCheck2 aria-hidden="true" />
        Create final cloud PDF
      </Button>
    );
  } else if (asksForCheckRun(summary.finalBlocked) && summary.rechecking.length === 0 && api.runChecks) {
    // The server waits for a check run that no count shows (QA 2026-10-16): its own words, and
    // the run it asks for. Never "nothing needs you" while sign-off is blocked.
    icon = <Lock aria-hidden="true" className="size-4 text-muted-foreground" />;
    headline = left.length > 0 ? left.join(' · ') : 'The checks need to run again.';
    sub = askError ? <span role="alert">{askError}</span> : summary.finalBlocked;
    action = (
      <span className="flex flex-col gap-2 sm:flex-row">
        <Button variant="outline" onClick={() => void runChecks()} disabled={asking}>
          {asking ? <Loader2 aria-hidden="true" className="animate-spin motion-reduce:animate-none" /> : <RotateCcw aria-hidden="true" />}
          Run the checks again
        </Button>
        <Button disabled>
          <FileCheck2 aria-hidden="true" />
          Create final cloud PDF
        </Button>
      </span>
    );
  } else {
    icon = <Lock aria-hidden="true" className="size-4 text-muted-foreground" />;
    // Plain words with the header's own counts (#1394). The server's sentence is shown only when
    // there is no count to say what is left; it never decides what the bar says is open.
    headline = left.length > 0 ? left.join(' · ') : 'Not ready for sign-off yet.';
    sub = left.length > 0 ? 'The final cloud PDF unlocks once these are settled.' : summary.finalBlocked;
    action = (
      <Button disabled>
        <FileCheck2 aria-hidden="true" />
        Create final cloud PDF
      </Button>
    );
  }

  const carried = facts !== null && facts !== 'loading' ? facts.carried : [];
  return (
    <div
      data-slot="final-bar"
      data-signed={signed || undefined}
      className="sticky bottom-0 z-10 -mx-4 mt-10 border-t border-border bg-background px-4 py-3 shadow-[0_-8px_24px_-16px_rgb(0_0_0/0.25)] sm:-mx-6 sm:px-6 sm:py-4 supports-[backdrop-filter]:bg-background/90 supports-[backdrop-filter]:backdrop-blur"
    >
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between sm:gap-6">
        <div className="flex min-w-0 items-start gap-3">
          <span className="mt-0.5 flex size-5 shrink-0 items-center justify-center">{icon}</span>
          <div className="flex min-w-0 flex-col gap-0.5">
            <p role="status" className="text-[15px] leading-6 font-medium text-foreground">{headline}</p>
            {/* What is left, and what went wrong, stay on a phone too; the longer sentences are for wider screens. */}
            {sub && <p className={locked || phase.kind === 'error' ? 'text-[13px] leading-5 text-muted-foreground' : 'hidden text-[13px] leading-5 text-muted-foreground sm:block'}>{sub}</p>}
          </div>
        </div>
        {action && <div className="flex shrink-0 [&>button]:w-full sm:[&>button]:w-auto [&>span]:w-full sm:[&>span]:w-auto [&>span>button]:w-full sm:[&>span>button]:w-auto">{action}</div>}
      </div>

      <Dialog open={dialog} onOpenChange={(next) => !signing && setDialog(next)}>
        <DialogContent data-slot="final-dialog" className="font-sans sm:max-w-lg">
          <DialogHeader>
            <DialogTitle className="text-[20px] leading-7">Sign off and create the final cloud PDF?</DialogTitle>
            <DialogDescription className="text-[15px] leading-6">
              {facts === 'loading' ? (
                'Checking who is signing…'
              ) : facts && facts.signer ? (
                <>
                  You are signing as <strong className="text-foreground">{facts.signer}</strong>.
                </>
              ) : (
                'The sign-off is recorded under your sign-in.'
              )}
            </DialogDescription>
          </DialogHeader>
          <ul className="flex list-disc flex-col gap-1.5 pl-5 text-[15px] leading-6">
            <li>
              Every result of revision <span className="num">{revision ?? '—'}</span> is approved as it stands now.
            </li>
            <li>The final cloud PDF is made next, with every item marked as you left it.</li>
            <li>
              <strong>This cannot be undone.</strong> A change after sign-off needs a new revision of the drawings.
            </li>
          </ul>
          {carried.length > 0 && (
            <section aria-label="Answers carried from an earlier revision" className="flex flex-col gap-2 rounded-lg border border-border p-3 text-[13px] leading-5">
              <p className="font-medium text-foreground">Answers carried from an earlier revision</p>
              <ul className="flex list-disc flex-col gap-1 pl-5 text-muted-foreground">
                {carried.map((item) => (
                  <li key={item.rowId}>{item.kind === 'no-countertop' ? item.words : <>{item.label}: {item.words}</>}</li>
                ))}
              </ul>
              <label className="flex items-center gap-2 text-foreground">
                <input type="checkbox" className="size-4 accent-current" checked={stillApply} onChange={(event) => setStillApply(event.target.checked)} />
                These answers still apply to this revision
              </label>
            </section>
          )}
          {signError && (
            <p role="alert" className="rounded-lg border border-outcome-fail-fg/40 bg-outcome-fail-bg px-3 py-2 text-[13px] leading-5 text-outcome-fail-fg">
              Nothing was signed: {signError}
            </p>
          )}
          <DialogFooter className="gap-2">
            <DialogClose asChild>
              <Button variant="ghost" disabled={signing}>
                Keep reviewing
              </Button>
            </DialogClose>
            <Button onClick={() => void signOff()} disabled={signing || facts === 'loading' || (carried.length > 0 && !stillApply)}>
              {signing && <Loader2 aria-hidden="true" className="animate-spin motion-reduce:animate-none" />}
              {signing ? 'Signing off…' : 'Sign off and create'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
