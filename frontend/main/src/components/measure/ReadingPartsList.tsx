import { useState, type ReactNode } from 'react';

import {
  isTheSuggestion,
  linkLabel,
  partName,
  partsStillToLink,
  readingChoiceLabel,
  readingLabel,
  readingOn,
  startingReading,
  type LinkDrawing,
  type LinkPart,
  type ReadingLinks,
} from './readingPartChoices.js';
import { GvMarksWarning } from './GvMarksWarning.js';
import type { DecisionFeedback } from './decisionFeedback.js';
import { Button } from '@/components/ui/button';
import { Caution, DecisionLine, Hint, SELECT_CLASS, StepSection } from './wizard-ui.js';
import { countWords } from './wizardWords.js';

/**
 * Which confirmed reading is each confirmed part's width: what the computer suggests, and a person
 * deciding each part's link (#913).
 *
 * **Why a person is asked.** A check that adds up a run needs each part's width, and a width is a
 * reading the drawing prints, never a length measured off it. The computer suggests the reading
 * whose dimension line (or, where no line was found, the box it is printed in) reaches both ends of
 * the part; a person confirms it, picks another reading, or takes the link back.
 *
 * **One part at a time.** Each part has its own list and buttons, and there is deliberately no
 * "confirm all".
 *
 * **Each part shows its picture (#897)**, the one cut for the suggestion it was confirmed from, so
 * the person picking its width sees the part; a part with none says so. A picture that shows GV's
 * own coloured marks says so under it (#921), as in "Parts of each drawing".
 */
export function ReadingPartsList({
  links,
  saving,
  feedback,
  renderPicture,
  onConfirm,
  onWithdraw,
}: {
  links: ReadingLinks;
  /** The part being saved, so its buttons cannot be pressed twice. */
  saving: string | null;
  feedback?: Readonly<Record<string, DecisionFeedback>>;
  /** The part's picture, for a part that has one. */
  renderPicture: (drawing: LinkDrawing, part: LinkPart) => ReactNode;
  onConfirm: (part: LinkPart, readingId: string) => void;
  onWithdraw: (part: LinkPart) => void;
}) {
  const open = partsStillToLink(links);
  return (
    <StepSection
      id="reading-parts-title"
      slot="reading-parts"
      title="Which reading is each part's width"
      line={
        <strong className="font-medium text-foreground">
          {open === 0 ? 'Nothing left to link.' : `${countWords(open, 'part')} still without a reading.`}
        </strong>
      }
      tipLabel="About width links"
      tip={
        <>
          <p>For each part you confirmed in step 1, the computer suggests the confirmed reading whose dimension line reaches both ends of the part. A part&apos;s width is one reading.</p>
          <p>A suggestion counts for nothing until you decide it. Confirm it, pick another reading on the same drawing and confirm that instead, or take a link back.</p>
        </>
      }
    >
      {links.why_not && <Hint>{links.why_not}</Hint>}
      {links.drawings.map((drawing) => (
        <article className="flex flex-col gap-3" key={drawing.view_id}>
          <h4 className="text-sm font-medium">Page {drawing.page_index + 1}: the vendor&apos;s drawing</h4>
          {drawing.why_not && <Hint>{drawing.why_not}</Hint>}
          {drawing.readings.length === 0 && (
            <Hint>No reading on this drawing is confirmed yet: confirm its readings in step 3 (Values) first.</Hint>
          )}
          <ol className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
            {drawing.parts.map((part) => (
              <PartLinkRow
                // A new decision starts the pick again from what was decided.
                key={`${part.item_id}:${part.links.map((link) => link.decided_at).join(',')}`}
                drawing={drawing}
                part={part}
                saving={saving === part.item_id}
                feedback={feedback?.[part.item_id]}
                picture={part.has_picture && part.proposal_id ? renderPicture(drawing, part) : null}
                onConfirm={onConfirm}
                onWithdraw={onWithdraw}
              />
            ))}
          </ol>
        </article>
      ))}
    </StepSection>
  );
}

function PartLinkRow({
  drawing,
  part,
  saving,
  feedback,
  picture,
  onConfirm,
  onWithdraw,
}: {
  drawing: LinkDrawing;
  part: LinkPart;
  saving: boolean;
  feedback?: DecisionFeedback;
  picture: ReactNode;
  onConfirm: (part: LinkPart, readingId: string) => void;
  onWithdraw: (part: LinkPart) => void;
}) {
  const [picked, setPicked] = useState<string>(() => startingReading(part));
  const suggestion = part.suggestion;
  const suggested = readingOn(drawing, suggestion?.reading_id);
  const name = partName(part);
  // Only a reading still on the drawing: one corrected since the page was loaded is never sent.
  const pickable = readingOn(drawing, picked) !== null;
  const pending = saving || feedback?.kind === 'saving';
  const canConfirm = drawing.can_confirm && pickable && !pending;
  const pickId = `link-reading-${part.item_id}`;

  return (
    <li
      data-slot="reading-part"
      data-decided={part.links.length > 0}
      className="flex min-w-0 flex-col gap-3 rounded-xl border bg-card p-3 data-[decided=true]:bg-muted/40"
    >
      <div className="flex flex-col gap-2">
        {picture ?? <Hint className="rounded-lg border border-dashed p-3">No picture of this part is stored yet.</Hint>}
        {picture && <GvMarksWarning marks={part.picture_gv_marks} />}
      </div>
      <div className="flex flex-col gap-0.5">
        <p className="text-sm font-medium">{name}</p>
        {suggestion && (
          <>
            <Hint>{suggested ? `Suggested width: ${readingLabel(suggested)}.` : 'No reading is suggested.'}</Hint>
            <Hint>{suggestion.said}</Hint>
          </>
        )}
        <p className="text-xs font-medium">{linkLabel(drawing, part)}</p>
        {part.links
          .filter((link) => !link.read && link.why_not_read)
          .map((link) => (
            <Caution key={link.reading_id}>{link.why_not_read}</Caution>
          ))}
      </div>
      <div className="flex flex-col gap-1">
        <label htmlFor={pickId} className="text-xs text-muted-foreground">
          Its width is the reading
        </label>
        <select
          id={pickId}
          className={SELECT_CLASS}
          value={picked}
          disabled={!drawing.can_confirm || pending || drawing.readings.length === 0}
          onChange={(event) => setPicked(event.target.value)}
        >
          <option value="">Pick a reading</option>
          {drawing.readings.map((reading) => (
            <option key={reading.reading_id} value={reading.reading_id}>
              {readingChoiceLabel(drawing, part, reading)}
            </option>
          ))}
        </select>
      </div>
      <div
        className="flex flex-wrap gap-2"
        role="group"
        aria-label={`The reading for ${name.toLowerCase()} on page ${drawing.page_index + 1}`}
      >
        <Button
          type="button"
          size="sm"
          variant="outline"
          disabled={!canConfirm}
          onClick={() => onConfirm(part, picked)}
        >
          {isTheSuggestion(part, picked) ? 'Confirm the suggested reading' : 'Confirm the picked reading'}
        </Button>
        <Button
          type="button"
          size="sm"
          variant="ghost"
          disabled={pending || part.links.length === 0}
          onClick={() => onWithdraw(part)}
        >
          Take the link back
        </Button>
      </div>
      <DecisionLine feedback={feedback} />
    </li>
  );
}
