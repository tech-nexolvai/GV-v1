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

export const PART_KINDS: readonly PartKind[] = ['cabinet', 'filler', 'countertop'];

export const KIND_LABEL: Record<PartKind, string> = {
  cabinet: 'Cabinet',
  filler: 'Filler',
  countertop: 'Countertop',
};

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
  if (decision.decision === 'withdrawn') return `Not a part, said ${decision.decided_by}.`;
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
