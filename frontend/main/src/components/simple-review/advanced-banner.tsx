import { ArrowLeft } from 'lucide-react';

import { Button } from '@/components/ui/button';

/** Above the old tabs (#1343): says where the reviewer is, and the way back to the simple page. */
export function AdvancedBanner({ onBack }: { onBack: () => void }) {
  return (
    <div data-tw data-slot="advanced-banner" className="flex flex-wrap items-center gap-3 border-b px-4 py-2 font-sans text-[13px] text-muted-foreground sm:px-6">
      <span>Advanced view: every tab and step, for admins.</span>
      <Button size="xs" variant="ghost" onClick={onBack}>
        <ArrowLeft aria-hidden="true" />
        Back to the simple review
      </Button>
    </div>
  );
}
