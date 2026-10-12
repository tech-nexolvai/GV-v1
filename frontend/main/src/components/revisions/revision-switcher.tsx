/**
 * The header's revision switcher (#1180): "Revision 2 of 2", and a menu of every revision with where
 * it stands in words. Choosing an earlier one shows it read-only; the newest is where work happens.
 * Shown only once a review has more than one revision.
 */

import { ChevronDown, History } from 'lucide-react';
import type { Revision } from '@/api/client';
import { Button } from '@/components/ui/button';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { cn } from '@/lib/utils';
import { revisionStatus } from '@/lib/revisions';

export function RevisionSwitcher({
  revisions,
  viewing,
  onView,
  className,
}: {
  revisions: readonly Revision[];
  /** The revision number on screen. */
  viewing: number;
  onView: (revisionNumber: number) => void;
  className?: string;
}) {
  if (revisions.length < 2) return null;
  const total = revisions.length;
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          variant="outline"
          size="sm"
          className={cn('font-sans', className)}
          aria-label={`Revision ${viewing} of ${total}. Choose a revision`}
          data-slot-name="revision-switcher"
        >
          <History aria-hidden="true" />
          <span>
            Revision <span className="num">{viewing}</span> of <span className="num">{total}</span>
          </span>
          <ChevronDown aria-hidden="true" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="min-w-56 font-sans">
        <DropdownMenuLabel className="text-xs text-muted-foreground">Revisions of this review</DropdownMenuLabel>
        <DropdownMenuRadioGroup value={String(viewing)} onValueChange={(value) => onView(Number(value))}>
          {[...revisions].reverse().map((revision) => (
            <DropdownMenuRadioItem key={revision.revision_id} value={String(revision.revision_number)}>
              <span className="flex flex-col">
                <span>
                  Revision <span className="num">{revision.revision_number}</span>
                </span>
                <span className="text-xs text-muted-foreground">{revisionStatus(revision)}</span>
              </span>
            </DropdownMenuRadioItem>
          ))}
        </DropdownMenuRadioGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
