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
  renderPicture,
  onConfirm,
  onWithdraw,
}: {
  links: ReadingLinks;
  /** The part being saved, so its buttons cannot be pressed twice. */
  saving: string | null;
  /** The part's picture, for a part that has one. */
  renderPicture: (drawing: LinkDrawing, part: LinkPart) => ReactNode;
  onConfirm: (part: LinkPart, readingId: string) => void;
  onWithdraw: (part: LinkPart) => void;
}) {
  const open = partsStillToLink(links);
  return (
    <section className="enter-values__section drawing-parts" aria-labelledby="reading-parts-title">
      <h2 id="reading-parts-title">Which reading is each part&apos;s width</h2>
      <p className="enter-values__hint">
        For each part you confirmed above, the computer suggests the confirmed reading whose
        dimension line reaches both ends of the part. A suggestion counts for nothing until you
        decide it. Confirm it, pick another reading on the same drawing and confirm that instead, or
        take a link back. A part&apos;s width is one reading.{' '}
        <strong>{open === 0 ? 'Nothing left to link.' : `${open} still without a reading.`}</strong>
      </p>
      {links.why_not && <p className="drawing-parts__why-not">{links.why_not}</p>}
      {links.drawings.map((drawing) => (
        <article className="drawing-parts__drawing" key={drawing.view_id}>
          <h3>Page {drawing.page_index + 1}: the vendor&apos;s drawing</h3>
          {drawing.why_not && <p className="drawing-parts__why-not">{drawing.why_not}</p>}
          {drawing.readings.length === 0 && (
            <p className="enter-values__hint">
              No reading on this drawing is confirmed yet. Say what its readings are under
              &quot;Measurements&quot; first; each part&apos;s width can be linked after that.
            </p>
          )}
          <ol className="drawing-parts__list">
            {drawing.parts.map((part) => (
              <PartLinkRow
                // A new decision starts the pick again from what was decided.
                key={`${part.item_id}:${part.links.map((link) => link.decided_at).join(',')}`}
                drawing={drawing}
                part={part}
                saving={saving === part.item_id}
                picture={part.has_picture && part.proposal_id ? renderPicture(drawing, part) : null}
                onConfirm={onConfirm}
                onWithdraw={onWithdraw}
              />
            ))}
          </ol>
        </article>
      ))}
    </section>
  );
}

function PartLinkRow({
  drawing,
  part,
  saving,
  picture,
  onConfirm,
  onWithdraw,
}: {
  drawing: LinkDrawing;
  part: LinkPart;
  saving: boolean;
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
  const canConfirm = drawing.can_confirm && pickable && !saving;

  return (
    <li className="drawing-parts__item" data-decided={part.links.length > 0}>
      <div className="drawing-parts__picture">
        {picture ?? <p className="drawing-parts__no-picture">No picture of this part is stored yet.</p>}
        {picture && <GvMarksWarning marks={part.picture_gv_marks} />}
      </div>
      <div className="drawing-parts__facts">
        <strong>{name}</strong>
        {suggestion && (
          <>
            <span>
              {suggested ? `Suggested width: ${readingLabel(suggested)}.` : 'No reading is suggested.'}
            </span>
            <span>{suggestion.said}</span>
          </>
        )}
        <span className="drawing-parts__decision">{linkLabel(drawing, part)}</span>
        {part.links
          .filter((link) => !link.read && link.why_not_read)
          .map((link) => (
            <span key={link.reading_id} className="countertop-runs__warning" role="status">
              {link.why_not_read}
            </span>
          ))}
      </div>
      <label className="drawing-parts__code">
        Its width is the reading
        <select
          value={picked}
          disabled={!drawing.can_confirm || saving || drawing.readings.length === 0}
          onChange={(event) => setPicked(event.target.value)}
        >
          <option value="">Pick a reading</option>
          {drawing.readings.map((reading) => (
            <option key={reading.reading_id} value={reading.reading_id}>
              {readingChoiceLabel(drawing, part, reading)}
            </option>
          ))}
        </select>
      </label>
      <div
        className="drawing-parts__choices"
        role="group"
        aria-label={`The reading for ${name.toLowerCase()} on page ${drawing.page_index + 1}`}
      >
        <button
          type="button"
          className="btn btn--sm btn--subtle"
          disabled={!canConfirm}
          onClick={() => onConfirm(part, picked)}
        >
          {isTheSuggestion(part, picked) ? 'Confirm the suggested reading' : 'Confirm the picked reading'}
        </button>
        <button
          type="button"
          className="btn btn--sm btn--subtle"
          disabled={saving || part.links.length === 0}
          onClick={() => onWithdraw(part)}
        >
          Take the link back
        </button>
      </div>
    </li>
  );
}
