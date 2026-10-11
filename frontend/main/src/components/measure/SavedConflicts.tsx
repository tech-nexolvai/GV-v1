import { AlertTriangle } from 'lucide-react';

import { Button } from '@/components/ui/button';
import type { SavedChange } from '@/lib/saved-values';

/**
 * Values someone else saved after this form was opened (#1183), found by the re-read before a save.
 * The save stopped; each one needs the reviewer's choice: keep what this screen shows, or take theirs.
 */
export function SavedConflicts({
  conflicts,
  labelOf,
  onChoose,
}: {
  conflicts: readonly SavedChange[];
  labelOf: (change: SavedChange) => string;
  onChoose: (change: SavedChange, keepMine: boolean) => void;
}) {
  if (conflicts.length === 0) return null;
  return (
    <div role="alert" data-part="saved-conflicts" className="flex flex-col gap-2 text-sm text-outcome-review-fg">
      <p className="flex items-start gap-1.5">
        <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
        <span>Nothing was saved: these values changed since you opened this page. Choose for each, then save again.</span>
      </p>
      <ul className="flex flex-col gap-2">
        {conflicts.map((change) => (
          <li key={change.id} className="flex flex-wrap items-center gap-2 text-foreground" data-conflict={change.id}>
            <span className="min-w-0">
              <span className="font-medium">{labelOf(change)}</span>: Saved by {change.setBy} since you opened this:{' '}
              <span className="num">{change.text}</span>. Keep yours?
            </span>
            <Button type="button" size="xs" variant="outline" onClick={() => onChoose(change, true)}>
              Keep mine
            </Button>
            <Button type="button" size="xs" variant="outline" onClick={() => onChoose(change, false)}>
              Use theirs
            </Button>
          </li>
        ))}
      </ul>
    </div>
  );
}
