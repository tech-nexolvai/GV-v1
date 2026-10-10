import type { ArchitectResult } from '@/api/client';
import { aiPickWords, matchOf, matchWords, viewHeading } from '@/lib/architect';
import { ArchitectViewLink } from './architect-line';

/**
 * Which of the architect's views a countertop was matched with, and on whose judgment (#1168), for a
 * row's details. Every word about the match is the server's (`match.reason`, `match.judgments`, the AIs'
 * own "why"); only the state's short name is the screen's. Nothing on a combined-sheet set.
 */
export function ArchitectMatchDetails({ result, onOpenView }: { result: ArchitectResult; onOpenView?: () => void }) {
  const match = matchOf(result);
  if (!match) return null;
  const view = match.matched_view;
  // An AI's pick names a view; only the matched one is known here, by its printed heading.
  const viewName = (viewId: string) => (view && view.view_id === viewId ? viewHeading(view) : 'another view');
  return (
    <div data-slot="architect-match-details" data-match={match.status} className="flex max-w-2xl flex-col gap-1 text-xs">
      <p className="font-medium">The architect&apos;s view</p>
      <p>{matchWords(result)}</p>
      {view && <p className="text-muted-foreground">{viewHeading(view)}{view.scale_note && <> · <span className="num">{view.scale_note}</span></>}</p>}
      <ArchitectViewLink result={result} onOpen={onOpenView} className="self-start" />
      {match.judgments && <p className="text-muted-foreground">Rests on: {match.judgments}</p>}
      {match.ai_picks.length > 0 && (
        <ul className="flex flex-col gap-0.5 text-muted-foreground" aria-label="What the AIs said">
          {match.ai_picks.map((pick) => (
            <li key={pick.model_label}>
              <span className="text-foreground">{pick.model_label}</span> {aiPickWords(pick, viewName)}
              {pick.why && <>: “{pick.why}”</>}
            </li>
          ))}
        </ul>
      )}
      {match.reason && <p className="text-muted-foreground">{match.reason}</p>}
    </div>
  );
}
