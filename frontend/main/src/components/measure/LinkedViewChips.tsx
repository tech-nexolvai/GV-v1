/**
 * The views of the drawing tied to a countertop, each with a small chip saying its kind (#1295).
 *
 * The depth and sink reader reads a countertop's depth and sink on its plan or section view, and ties
 * each such view to one countertop (`item_view_links`). This shows which views those are, so a person
 * can see where a depth was read. Nothing is shown when the reader never read the set.
 */
import { Badge } from '@/components/ui/badge';
import { VIEW_CHIP, type LinkedView } from '@/lib/depth-readings';

export function LinkedViewChips({ views }: { views: readonly LinkedView[] }) {
  if (views.length === 0) return null;
  return (
    <div data-slot="linked-views" className="flex flex-wrap items-center gap-1.5 text-xs">
      <span className="text-muted-foreground">Views of this countertop:</span>
      {views.map((view, index) => (
        <Badge
          key={`${view.view}-${view.page_number}-${index}`}
          variant="outline"
          data-view={view.view}
          title={`${view.heading ? `Headed “${view.heading}”. ` : ''}${view.by_person ? 'Tied by a person.' : 'Tied by code and both AI readers.'}`}
        >
          {VIEW_CHIP[view.view]} · page <span className="num">{view.page_number}</span>
        </Badge>
      ))}
    </div>
  );
}
