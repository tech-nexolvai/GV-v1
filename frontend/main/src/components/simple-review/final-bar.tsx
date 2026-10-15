import { useEffect, useRef, useState } from 'react';
import { CheckCheck, CircleAlert, Download, FileCheck2, Loader2, Lock, RotateCcw } from 'lucide-react';

import type { FinalCloudApi, SignOffFacts, SignedFilesStatus } from '@/api/final-cloud';
import type { ReviewSummary } from '@/lib/review-items';
import { cn } from '@/lib/utils';
import { Button } from '@/components/ui/button';
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';

/** How often the bar asks whether the signed files are ready, while they are being made. */
export const FINAL_POLL_MS = 3000;

type Phase =
  | { kind: 'unsigned' }
  | { kind: 'checking' }
  | { kind: 'signed'; files: SignedFilesStatus }
  | { kind: 'error'; message: string; signed: boolean };

function words(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/**
 * The bottom of the page (#1368): the final cloud PDF. It is honest about what blocks it: the
 * server's own reason (`blocked_reason`) and what is left, counted from the lists. The button is
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
}: {
  summary: ReviewSummary;
  /** What is left, in words ("2 items to answer"), from the lists the server sent. */
  left: readonly string[];
  api: FinalCloudApi;
  projectId: string | null;
  packageId: string;
  revision: number | null;
  /** The package was already signed off when the page loaded (its state is APPROVED). */
  approved: boolean;
  /** After a sign-off: the page reloads its lists (every answer is now closed). */
  onSigned: () => void;
}) {
  const [signedNow, setSignedNow] = useState(false);
  const [files, setFiles] = useState<SignedFilesStatus | null>(null);
  const [statusAsk, setStatusAsk] = useState(0);
  const [failure, setFailure] = useState<string | null>(null);
  const [dialog, setDialog] = useState(false);
  const [facts, setFacts] = useState<SignOffFacts | null | 'loading'>(null);
  const [stillApply, setStillApply] = useState(false);
  const [signing, setSigning] = useState(false);
  const [signError, setSignError] = useState<string | null>(null);
  const [downloading, setDownloading] = useState(false);
  const mounted = useRef(true);
  useEffect(() => () => {
    mounted.current = false;
  }, []);

  const signed = approved || signedNow;

  // Once signed off (before the page opened, or here): where the signed files are.
  useEffect(() => {
    if (!signed || projectId === null) return;
    let current = true;
    api.status(projectId, packageId).then(
      (answer) => {
        if (!current) return;
        setFiles(answer);
        setFailure(null);
      },
      (error: unknown) => current && setFailure(`Where the final cloud PDF is could not be read: ${words(error)}`),
    );
    return () => {
      current = false;
    };
  }, [api, signed, projectId, packageId, statusAsk]);

  // While the files are being made, ask again every few seconds.
  useEffect(() => {
    if (files !== 'preparing') return;
    const timer = window.setTimeout(() => setStatusAsk((n) => n + 1), FINAL_POLL_MS);
    return () => window.clearTimeout(timer);
  }, [files, statusAsk]);

  const phase: Phase = failure !== null
    ? { kind: 'error', message: failure, signed }
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
      setDialog(false);
      setFiles('preparing');
      setSignedNow(true);
      onSigned();
    } catch (error) {
      setSignError(words(error));
    } finally {
      if (mounted.current) setSigning(false);
    }
  }

  async function prepare() {
    if (projectId === null) return;
    try {
      setFiles(await api.prepare(projectId, packageId));
      setFailure(null);
    } catch (error) {
      setFailure(`The final cloud PDF could not be requested: ${words(error)}`);
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
      setFailure(`The final cloud PDF could not be downloaded: ${words(error)}`);
    } finally {
      if (mounted.current) setDownloading(false);
    }
  }

  // ── What the bar says ──
  let icon: React.ReactNode;
  let headline: React.ReactNode;
  let sub: React.ReactNode = null;
  let action: React.ReactNode;

  if (phase.kind === 'error') {
    icon = <CircleAlert aria-hidden="true" className="size-4 text-outcome-fail-fg" />;
    headline = <span className="text-outcome-fail-fg">{phase.signed ? 'Signed off, but something went wrong' : 'Something went wrong'}</span>;
    sub = phase.message;
    action = (
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
        <Button onClick={() => void prepare()}>
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
  } else {
    icon = <Lock aria-hidden="true" className="size-4 text-muted-foreground" />;
    headline = summary.finalBlocked;
    sub = left.length > 0 ? left.join(' · ') : null;
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
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between sm:gap-6">
        <div className="flex min-w-0 items-start gap-3">
          <span className="mt-0.5 flex size-5 shrink-0 items-center justify-center">{icon}</span>
          <div className="flex min-w-0 flex-col gap-0.5">
            <p role="status" className="text-[15px] leading-6 font-medium text-foreground">{headline}</p>
            {sub && <p className={cn('text-[13px] leading-5 text-muted-foreground')}>{sub}</p>}
          </div>
        </div>
        {action && <div className="flex shrink-0 [&>button]:w-full sm:[&>button]:w-auto">{action}</div>}
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
