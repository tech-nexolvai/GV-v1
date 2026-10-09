import { cn } from '@/lib/utils';

/**
 * A decision carried over from the previous check run (#1073): the re-run gave the same result, so the
 * reviewer's decision on it still stands. Said in words beside the decision, never by colour.
 */
export function CarriedOver({ className }: { className?: string }) {
  return (
    <span data-slot="carried-over" title="Carried over from the previous check run: this result came back unchanged." className={cn('text-xs font-normal text-muted-foreground', className)}>
      carried over
    </span>
  );
}
