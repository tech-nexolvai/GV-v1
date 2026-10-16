import { FileStack, FileText, Loader2, RotateCcw } from 'lucide-react';

import type { ReviewItemsHeader } from '@/api/review-items';
import { Button } from '@/components/ui/button';
import { Kbd } from './words';

/** Where a requested draft cloud PDF is: nothing asked yet, being made, downloaded, or a named failure. */
export type CloudState = { status: 'idle' } | { status: 'working' } | { status: 'ready' } | { status: 'error'; message: string };

function Count({ value }: { value: number }) {
  return <span className="num font-medium text-foreground">{value}</span>;
}

/**
 * The top of the page: the set's name, what the AI did on its own, how often it was right on the
 * numbers a person checked (#1386), and the two ways out to the drawings — both PDFs, and a draft
 * of the cloud PDF that can be made at any time. Every count and percentage is the server's
 * (`ReviewHeaderOut`); nothing here works one out. With nothing checked there is no accuracy at all,
 * never 0% or 100%.
 */
export function ReviewHeader({
  title,
  revision,
  header,
  onOpenPdfs,
  onDraftCloud,
  draft,
  signedOff,
}: {
  title: string;
  revision: number | null;
  header: ReviewItemsHeader;
  onOpenPdfs: () => void;
  /** Ask for the draft cloud PDF (W4b); null when it cannot be asked for here: the button says so instead of hiding. */
  onDraftCloud: (() => void) | null;
  draft: CloudState;
  /** The set is signed off: the draft is no longer made, the final cloud PDF is downloaded instead. */
  signedOff: boolean;
}) {
  const working = draft.status === 'working';
  return (
    <header data-slot="review-header" className="flex flex-col gap-4 pt-8 pb-6 sm:flex-row sm:flex-wrap sm:items-end sm:justify-between">
      <div className="flex min-w-0 flex-col gap-2">
        <h2 className="truncate text-[28px] leading-9 font-semibold tracking-[-0.015em] text-foreground">{title}</h2>
        <p data-testid="ai-read-line" className="text-[15px] leading-6 text-muted-foreground">
          AI read <Count value={header.pages_read} /> pages · <Count value={header.numbers} /> numbers ·{' '}
          <Count value={header.confirmed_by_ai} /> settled by both AIs
          {header.settled_percent !== null && (
            <>
              {' '}(<span className="num">{header.settled_percent}%</span>)
            </>
          )}{' '}
          · <Count value={header.need_you} /> not settled by the AIs
        </p>
        <p data-testid="accuracy-line" className="text-[13px] leading-5 text-muted-foreground">
          {header.readings_checked > 0 ? (
            <>
              Your answers so far: <Count value={header.readings_checked} /> · AI right on <Count value={header.ai_right} />
              {header.ai_right_percent !== null && (
                <>
                  {' '}(<span className="num">{header.ai_right_percent}%</span>)
                </>
              )}
            </>
          ) : (
            'Accuracy: shown after you check some numbers'
          )}
        </p>
        {header.settled_checked > 0 && (
          <p data-testid="settled-check-line" className="text-[13px] leading-5 text-muted-foreground">
            Of the numbers both AIs settled that you checked: <Count value={header.settled_right} /> right of{' '}
            <Count value={header.settled_checked} />
          </p>
        )}
        {revision !== null && (
          <p className="text-[13px] leading-5 text-muted-foreground">
            Revision <span className="num">{revision}</span>
          </p>
        )}
      </div>
      <div className="flex shrink-0 flex-wrap items-center gap-2">
        <Button variant="outline" onClick={onOpenPdfs}>
          <FileStack aria-hidden="true" />
          Open PDFs
          <Kbd>O</Kbd>
        </Button>
        <Button
          variant="outline"
          onClick={onDraftCloud ?? undefined}
          disabled={signedOff || onDraftCloud === null || working}
          title={
            signedOff
              ? 'Signed off: download the final cloud PDF instead.'
              : onDraftCloud === null
                ? 'The draft cloud PDF cannot be asked for here: no project is configured.'
                : 'A draft with every item marked as it stands now. Not signed off.'
          }
        >
          {working ? <Loader2 aria-hidden="true" className="animate-spin motion-reduce:animate-none" /> : <FileText aria-hidden="true" />}
          {working ? 'Making draft…' : 'Draft cloud PDF'}
        </Button>
      </div>
      {signedOff && (
        <p data-testid="draft-signed-off" className="text-[13px] text-muted-foreground sm:basis-full">
          Signed off. The draft is no longer made; download the final cloud PDF instead.
        </p>
      )}
      {working && (
        <p role="status" className="text-[13px] text-muted-foreground sm:basis-full">
          Making the draft cloud PDF. This takes a minute; it downloads by itself when ready.
        </p>
      )}
      {draft.status === 'ready' && (
        <p role="status" className="text-[13px] text-muted-foreground sm:basis-full">
          Draft cloud PDF downloaded. It is not signed off.
        </p>
      )}
      {draft.status === 'error' && (
        <div className="flex flex-wrap items-center gap-2 sm:basis-full">
          <p role="alert" className="text-[13px] text-outcome-fail-fg">
            The draft cloud PDF could not be made: {draft.message}
          </p>
          {onDraftCloud && !signedOff && (
            <Button size="sm" variant="outline" onClick={onDraftCloud}>
              <RotateCcw aria-hidden="true" />
              Retry
            </Button>
          )}
        </div>
      )}
    </header>
  );
}
