import { FileCheck2, Loader2, Lock } from 'lucide-react';

import type { ReviewSummary } from '@/lib/review-items';
import { Button } from '@/components/ui/button';
import type { CloudState } from './review-header';

/**
 * The bottom of the page: the final cloud PDF, locked until nothing is open. It always says why it is
 * locked, so the reviewer knows what is left without scrolling.
 */
export function FinalBar({ summary, final, onCreateFinal }: { summary: ReviewSummary; final: CloudState; onCreateFinal: () => void }) {
  const working = final.status === 'working';
  return (
    <div data-slot="final-bar" className="sticky bottom-0 z-10 -mx-6 mt-8 border-t border-border bg-background/95 px-6 py-4 backdrop-blur supports-[backdrop-filter]:bg-background/80">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p role="status" className="flex items-center gap-2 text-[13px] leading-5 text-muted-foreground">
          {summary.canFinalise ? (
            <>Everything is checked. The final cloud PDF marks each item as you left it.</>
          ) : (
            <>
              <Lock aria-hidden="true" className="size-3.5 shrink-0" />
              {summary.finalBlocked}
            </>
          )}
        </p>
        <Button onClick={onCreateFinal} disabled={!summary.canFinalise || working}>
          {working ? <Loader2 aria-hidden="true" className="animate-spin motion-reduce:animate-none" /> : <FileCheck2 aria-hidden="true" />}
          Create final cloud PDF
        </Button>
      </div>
      {final.status === 'error' && (
        <p role="alert" className="mt-2 text-[13px] text-outcome-fail-fg">
          The final cloud PDF could not be made: {final.message}
        </p>
      )}
    </div>
  );
}
