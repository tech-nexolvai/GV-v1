import type { components } from '../../api/schema';
import { countOf, type StepCount } from '../../lib/measure-steps';

/** Every confirmed part and its link, as `GET …/reading-parts` lists them (#913). */
export type ReadingLinks = components['schemas']['ReadingPartsOut'];

/** One drawing with a confirmed part: its parts, and the readings a link may name. */
export type LinkDrawing = components['schemas']['LinkDrawingOut'];

/** One confirmed part: the reading suggested as its width, and what a person decided. */
export type LinkPart = components['schemas']['PartLinkOut'];

/** A confirmed reading on the drawing, which a person may pick as a part's width. */
export type LinkReading = components['schemas']['LinkReadingOut'];

type Kind = components['schemas']['PartKind'];

/**
 * A part as "Parts of each drawing" names it: by its number in that list and the kind a person
 * confirmed. A part not listed there is still named by its kind rather than left blank.
 */
export function partName(part: { number: number | null; kind: Kind }): string {
  return part.number === null ? `An unnumbered ${part.kind}` : `Part ${part.number} (${part.kind})`;
}

/** The words for what a person confirmed a reading is, as the type is stored. */
function typeWords(semanticType: string): string {
  return semanticType.replaceAll('_', ' ');
}

/**
 * A reading as a person picks it: its exact value and what they confirmed it is. The value is the
 * reading's own, never a length measured off the drawing.
 */
export function readingLabel(reading: LinkReading): string {
  return `${reading.value}, confirmed as ${typeWords(reading.semantic_type)}`;
}

/** A reading's label in the list to pick from, saying when it is another part's width now. */
export function readingChoiceLabel(drawing: LinkDrawing, part: LinkPart, reading: LinkReading): string {
  const label = readingLabel(reading);
  if (reading.linked_to === null || reading.linked_to === part.item_id) return label;
  const other = drawing.parts.find((candidate) => candidate.item_id === reading.linked_to);
  const name = other ? partName(other).toLowerCase() : 'another part';
  return `${label} (now the width of ${name}; picking it moves it here)`;
}

/** The reading a part's link names, or the suggestion names, looked up on its drawing. */
export function readingOn(drawing: LinkDrawing, readingId: string | null | undefined) {
  return drawing.readings.find((reading) => reading.reading_id === readingId) ?? null;
}

/**
 * How many parts still have no reading linked, on the drawings where one can be linked. Nothing is
 * counted on a drawing no longer confirmed as the vendor's: the page says why instead.
 */
export function partsStillToLink(links: ReadingLinks): number {
  return links.drawings
    .filter((drawing) => drawing.can_confirm)
    .reduce((open, drawing) => open + drawing.parts.filter((part) => part.links.length === 0).length, 0);
}

/** For the Measurements step bar (#1061): the parts that can be linked, and how many are. */
export function linkCount(links: ReadingLinks): StepCount {
  const total = links.drawings.filter((drawing) => drawing.can_confirm).reduce((sum, drawing) => sum + drawing.parts.length, 0);
  return countOf(total, partsStillToLink(links));
}

/**
 * The reading a person starts from: the one linked, while it is read, or else the suggestion. Shown
 * picked for them to keep or change, never sent without their click. Empty when there is neither.
 */
export function startingReading(part: LinkPart): string {
  const read = part.links.find((link) => link.read);
  if (read) return read.reading_id;
  return part.suggestion?.reading_id ?? '';
}

/** Whether the picked reading is the one the computer suggests. Never true with no suggestion. */
export function isTheSuggestion(part: LinkPart, readingId: string): boolean {
  const suggested = part.suggestion?.reading_id;
  return Boolean(suggested) && suggested === readingId;
}

/** What a person last linked to a part, in a sentence. */
export function linkLabel(drawing: LinkDrawing, part: LinkPart): string {
  if (part.links.length === 0) return 'No reading is linked to this part yet.';
  return part.links
    .map((link) => {
      const reading = readingOn(drawing, link.reading_id);
      const named = reading ? readingLabel(reading) : 'a reading no longer on this drawing';
      return `Linked by ${link.decided_by}: ${named}.`;
    })
    .join(' ');
}
