import { FileStack, FileText, Loader2 } from 'lucide-react';

import type { ReviewItemsHeader } from '@/api/review-items';
import { Button } from '@/components/ui/button';
import { Kbd } from './words';

/** Where a requested cloud PDF is: nothing asked yet, being made, or a named failure. */
export type CloudState = { status: 'idle' } | { status: 'working' } | { status: 'error'; message: string };

function Count({ value }: { value: number }) {
  return <span className="num font-medium text-foreground">{value}</span>;
}

/**
 * The top of the page: the set's name, what the AI did on its own, and the two ways out to the
 * drawings — both PDFs, and a draft of the cloud PDF that can be made at any time.
 */
export function ReviewHeader({
  title,
  revision,
  header,
  onOpenPdfs,
  onDraftCloud,
  draft,
}: {
  title: string;
  revision: number | null;
  header: ReviewItemsHeader;
  onOpenPdfs: () => void;
  /** Null until the draft cloud PDF exists on this server (W4b): the button says so instead of hiding. */
  onDraftCloud: (() => void) | null;
  draft: CloudState;
}) {
  const working = draft.status === 'working';
  return (
    <header data-slot="review-header" className="flex flex-col gap-4 pt-8 pb-6 sm:flex-row sm:flex-wrap sm:items-end sm:justify-between">
      <div className="flex min-w-0 flex-col gap-2">
        <h2 className="truncate text-[28px] leading-9 font-semibold tracking-[-0.015em] text-foreground">{title}</h2>
        <p data-testid="ai-read-line" className="text-[15px] leading-6 text-muted-foreground">
          AI read <Count value={header.pages_read} /> pages · <Count value={header.numbers} /> numbers ·{' '}
          <Count value={header.confirmed_by_ai} /> confirmed by AI
        </p>
        {revision !== null && (
          <p className="text-[13px] leading-5 text-muted-foreground">
            Revision <span className="num">{revision}</span>
            {header.checked_by_you > 0 && (
              <>
                {' '}· <span className="num">{header.checked_by_you}</span> checked by you
              </>
            )}
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
          disabled={onDraftCloud === null || working}
          title={onDraftCloud === null ? 'The draft cloud PDF is not available on this server yet.' : 'A draft with every item marked as it stands now. Not signed off.'}
        >
          {working ? <Loader2 aria-hidden="true" className="animate-spin motion-reduce:animate-none" /> : <FileText aria-hidden="true" />}
          {working ? 'Making draft…' : 'Draft cloud PDF'}
        </Button>
      </div>
      {draft.status === 'error' && (
        <p role="alert" className="text-[13px] text-outcome-fail-fg sm:basis-full">
          The draft cloud PDF could not be made: {draft.message}
        </p>
      )}
    </header>
  );
}
