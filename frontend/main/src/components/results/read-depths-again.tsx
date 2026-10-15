/**
 * "Read depths again" (#1308), on a waiting depth or sink check: the depth and sink reader run on a
 * drawing set that was already read, for a set read before the reader existed or was switched on.
 *
 * Shown only when the server says the reader is switched on (`GV_DEPTH_READER`) and the check is one
 * the reader fills. It costs money, so it always asks first: what will be read, about how many
 * questions, how many already have a stored answer (free), and that spending stops at the drawing
 * set's AI budget. Refused requests and the last request's outcome are said in the server's words.
 * Nothing here decides a check: the checks run again once the depths are read.
 */
import { useEffect, useState } from 'react';

import { readDepthsAgain, type ReadDepthsAgainStatus } from '@/api/client';
import { projectId } from '@/api/config';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { questionsWords, readDepthsAgainStatus as statusOf } from '@/lib/read-depths-again';

export function ReadDepthsAgain({ packageId, checkId }: { packageId: string; checkId: string }) {
  const [status, setStatus] = useState<ReadDepthsAgainStatus | null>(null);
  const [open, setOpen] = useState(false);
  const [asking, setAsking] = useState(false);
  const [asked, setAsked] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    statusOf(packageId)
      .then((found) => {
        if (live) setStatus(found);
      })
      // Nothing to offer when the status cannot be read: the card stays as it was.
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, [packageId]);

  if (!status || !status.switched_on || !(status.checks ?? []).includes(checkId)) return null;
  const estimate = status.estimate ?? null;
  const last = status.last ?? null;

  async function ask() {
    if (asking) return;
    setAsking(true);
    setError(null);
    try {
      await readDepthsAgain(projectId(), packageId);
      setAsked(true);
      setOpen(false);
      setStatus(await statusOf(packageId, true));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setAsking(false);
    }
  }

  return (
    <div data-slot="read-depths-again" className="flex flex-col gap-1.5">
      <div className="flex flex-wrap items-center gap-2">
        <Button
          type="button"
          size="sm"
          variant="outline"
          disabled={!status.can_ask || asking}
          onClick={() => {
            setError(null);
            setOpen(true);
          }}
        >
          Read depths again…
        </Button>
        {!status.can_ask && status.refused && (
          <span className="text-xs text-muted-foreground" data-slot="read-depths-again-why">
            {status.refused}
          </span>
        )}
      </div>
      {asked && (
        <p className="text-xs text-muted-foreground" role="status">
          Asked. The depths are read in the background; the checks run again when it finishes.
        </p>
      )}
      {!asked && last && (
        <p className="text-xs text-muted-foreground" data-slot="read-depths-again-last" data-state={last.state}>
          Last request: {last.sentence}
        </p>
      )}
      {error && !open && (
        <p role="alert" className="text-xs text-outcome-fail-fg">
          {error}
        </p>
      )}
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>Read depths again?</DialogTitle>
            <DialogDescription>
              The depth and sink reader reads each countertop&apos;s plan and section views on this drawing set again. The
              rest of the set is not read again.
            </DialogDescription>
          </DialogHeader>
          {estimate && (
            <div className="flex flex-col gap-1.5 text-sm" data-slot="read-depths-again-estimate">
              <p>
                <span className="num">{estimate.countertops}</span> {estimate.countertops === 1 ? 'countertop' : 'countertops'} on{' '}
                <span className="num">{estimate.pages}</span> {estimate.pages === 1 ? 'page' : 'pages'}.{' '}
                {questionsWords(estimate)} to the AI readers
                {estimate.stored_answers > 0 && (
                  <>
                    , <span className="num">{estimate.stored_answers}</span> of them already answered (reused at no cost)
                  </>
                )}
                .
              </p>
              <p className="text-xs text-muted-foreground">{estimate.basis}</p>
            </div>
          )}
          <p className="text-sm font-medium" data-slot="read-depths-again-cost">
            This costs money: every new question is a paid AI call. Spending stops at this drawing set&apos;s AI budget.
          </p>
          {error && (
            <p role="alert" className="text-sm text-outcome-fail-fg">
              {error}
            </p>
          )}
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => setOpen(false)} disabled={asking}>
              Cancel
            </Button>
            <Button type="button" onClick={() => void ask()} disabled={asking}>
              {asking ? 'Asking…' : 'Read depths again (costs money)'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
