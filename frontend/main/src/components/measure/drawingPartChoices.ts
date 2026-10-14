import type { components } from '../../api/schema';
import { countOf, type StepCount } from '../../lib/measure-steps';

/** One drawing and its suggested parts, left to right, as `GET …/parts` lists it (#882). */
export type PartDrawing = components['schemas']['DrawingOut'];

/** One suggested part, with what a person last said about it. */
export type SuggestedPart = components['schemas']['PartOut'];

export type PartKind = components['schemas']['PartKind'];

export type PartPoint = components['schemas']['PointOut'];

/** What a part's picture was found to show of GV's own coloured marks when it was cut (#921). */
export type PictureGvMarks = components['schemas']['GvMarks'];

/**
 * The warning under a part's picture that shows GV's own coloured marks (#921): GV's marks baked into
 * the vendor's drawing, which the picture cannot leave out. The admin's words, 2026-10-04.
 */
export const GV_MARKS_WARNING =
  "This picture shows GV's own coloured marks; check the vendor's drawing itself.";

/**
 * Whether a picture carries the warning: only where the check found GV's marks in it. A picture
 * checked and found without them carries none, and so does one never checked (cut before the check
 * existed): the warning says only what the check found, and nothing is guessed either way.
 */
export function showsGvMarks(marks: PictureGvMarks | null | undefined): boolean {
  return marks === 'shown';
}

/** Why a person says a piece is not a part (#1246), kept on their decision. */
export type NotPartReason = components['schemas']['NotPartReason'];

export const PART_KINDS: readonly PartKind[] = ['cabinet', 'filler', 'countertop'];

/**
 * The answers to "what is it?" that say a piece is not a part (#1246), in the order they are offered.
 * "Side panel" is phase 5 decision 6: the end panels in the client's sink cabinet formula.
 */
export const NOT_PART_REASONS: readonly NotPartReason[] = ['appliance-space', 'side-panel', 'other'];

export const NOT_PART_REASON_LABEL: Record<NotPartReason, string> = {
  'appliance-space': 'Appliance space',
  'side-panel': 'Side panel',
  other: 'Something else',
};

export const KIND_LABEL: Record<PartKind, string> = {
  cabinet: 'Cabinet',
  filler: 'Filler',
  countertop: 'Countertop',
};

/**
 * What a suggestion Bala made from a countertop row says it is (#1236): code read the row the AI
 * readers found and suggested the countertop and each of its pieces, with the kind filled in.
 */
export const FROM_COUNTERTOP_LINE = 'Suggested by code from the countertop line';

/**
 * What a countertop code confirmed with no click says (#1244, ADR-0021 amendment): both AI readers
 * named its row, and code found the row wholly inside one drawing confirmed as the vendor's. A
 * person can always undo it, and a person's decision always wins.
 */
export const FOUND_BY_CODE = 'Found by code: two checks agreed';

/** Whether code, not a person, confirmed this part and that decision still stands (#1244). */
export function foundByCode(part: SuggestedPart): boolean {
  return part.decision?.decision === 'confirmed' && part.decision.by_code === true;
}

/** The reason, less the words the card already shows above it. */
export function reasonToShow(part: SuggestedPart): string {
  const prefix = `${FROM_COUNTERTOP_LINE}: `;
  if (!part.from_countertop_line || !part.reason.startsWith(prefix)) return part.reason;
  const rest = part.reason.slice(prefix.length);
  return rest.charAt(0).toUpperCase() + rest.slice(1);
}

/**
 * Whether a part may be confirmed as suggested with one click: one of Bala's, still undecided, and
 * only where the drawing names its kind (the countertop, or a piece with a printed word).
 */
export function oneClickKind(part: SuggestedPart): PartKind | null {
  return part.from_countertop_line && part.kind_named && !part.decision ? part.suggested_kind : null;
}

/**
 * Whether the person is asked what a piece is (review of #1243): one of Bala's pieces, still
 * undecided, that nothing printed names. Nothing is filled in for them.
 */
export function askWhatItIs(part: SuggestedPart): boolean {
  return part.from_countertop_line && !part.kind_named && !part.decision;
}

/** Whose drawing it is, as a person would say it. */
export function drawingLabel(drawing: PartDrawing): string {
  if (drawing.role === 'shop') return "the vendor's drawing";
  if (drawing.role === 'arch') return "the architect's drawing";
  return 'a drawing nobody has confirmed yet';
}

/**
 * How many suggestions still need a person, on the drawings where a part can be confirmed.
 *
 * A suggestion on a drawing nobody has confirmed as the vendor's cannot be confirmed yet, so it is
 * not counted: the drawing's own note says what to do first.
 */
export function stillToDecide(drawings: readonly PartDrawing[]): number {
  return drawings
    .filter((drawing) => drawing.can_confirm)
    .reduce((open, drawing) => open + drawing.parts.filter((part) => !part.decision).length, 0);
}

/** For the Measurements step bar (#1061): the suggestions that can be decided, and how many are. */
export function partCount(drawings: readonly PartDrawing[]): StepCount {
  const total = drawings.filter((drawing) => drawing.can_confirm).reduce((sum, drawing) => sum + drawing.parts.length, 0);
  return countOf(total, stillToDecide(drawings));
}

/** What a person last said about a part, in a sentence. */
export function decisionLabel(part: SuggestedPart): string {
  const decision = part.decision;
  if (!decision) return 'Not decided yet.';
  if (decision.decision === 'withdrawn') {
    const reason = decision.not_part_reason;
    if (!reason) return `Not a part, said ${decision.decided_by}.`;
    return `Not a part (${NOT_PART_REASON_LABEL[reason].toLowerCase()}), said ${decision.decided_by}.`;
  }
  if (decision.by_code) {
    const found = decision.kind ? KIND_LABEL[decision.kind].toLowerCase() : 'part';
    return `Confirmed as a ${found} by code, not by a person.`;
  }
  const kind = decision.kind ? KIND_LABEL[decision.kind].toLowerCase() : 'part';
  const code = decision.code === null ? 'no code' : `code “${decision.code}”`;
  return `Confirmed as a ${kind}, ${code}, by ${decision.decided_by}.`;
}

/**
 * The code a person starts from: the one they last confirmed, or else the one read on the drawing.
 * Shown in an input for them to keep or correct, never sent without their click.
 */
export function startingCode(part: SuggestedPart): string {
  if (part.decision?.decision === 'confirmed') return part.decision.code ?? '';
  return part.suggested_code ?? '';
}

/**
 * The code as it is sent: exactly what was typed, or null for an empty box.
 *
 * Never trimmed. The server keeps a code verbatim, because the code is what the drawing prints and
 * a reviewer checking it is looking at the sheet.
 */
export function codeToSend(typed: string): string | null {
  return typed === '' ? null : typed;
}

/** One end a person can pick for a part they add. */
export interface EndChoice {
  key: string;
  label: string;
  point: PartPoint;
}

/**
 * The ends a person can give a part they add: the two ends of every part listed on the drawing.
 *
 * In the list's left-to-right order, and one choice per point, so the right end of one part and the
 * left end of the next share an entry when the drawing draws them at the same place. Points are
 * compared as the exact text the server sent, never as numbers.
 */
export function endChoices(drawing: PartDrawing): EndChoice[] {
  const choices = new Map<string, EndChoice>();
  for (const part of drawing.parts) {
    const ends: [string, PartPoint | null][] = [
      [`left end of ${part.position}`, part.left_end],
      [`right end of ${part.position}`, part.right_end],
    ];
    for (const [label, point] of ends) {
      if (!point) continue;
      const key = `${point.x},${point.y}`;
      const existing = choices.get(key);
      choices.set(key, existing ? { ...existing, label: `${existing.label}, ${label}` } : { key, label, point });
    }
  }
  return [...choices.values()];
}
