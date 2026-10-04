import type { components } from '../../api/schema';

/** Every confirmed countertop and its run, as `GET …/countertop-runs` lists them (#893). */
export type RunsList = components['schemas']['RunsOut'];

/** One drawing with a confirmed part, its countertops and the parts a run may hold. */
export type RunDrawing = components['schemas']['RunDrawingOut'];

/** One confirmed countertop: the run suggested beneath it, and what a person decided. */
export type RunCountertop = components['schemas']['CountertopOut'];

/** A confirmed cabinet or filler a run may hold. */
export type RunPart = components['schemas']['RunPartOut'];

type Kind = components['schemas']['PartKind'];

export function wallLayoutLabel(value: string): string {
  if (value === 'back_left_right') return 'Walls at both ends';
  if (value === 'back_only') return 'Back wall only';
  if (value === 'island') return 'Island';
  return value;
}

/**
 * A part as "Parts of each drawing" names it: by its number in that list and the kind a person
 * confirmed. A part not listed there (rare: one confirmed before its suggestion was replaced) is
 * still named by its kind rather than left blank.
 */
export function partLabel(part: { number: number | null; kind: Kind | null }): string {
  const kind = part.kind ?? 'part';
  return part.number === null ? `an unnumbered ${kind}` : `part ${part.number} (${kind})`;
}

/**
 * How many countertops still need a person to decide their run, on the drawings where a run can be
 * confirmed. Nothing is counted while no run can be suggested: the page says why instead.
 */
export function runsStillToDecide(runs: RunsList): number {
  if (!runs.can_suggest) return 0;
  return runs.drawings
    .filter((drawing) => drawing.can_confirm)
    .reduce((open, drawing) => open + drawing.countertops.filter((top) => !top.decision).length, 0);
}

/**
 * The parts a person starts from: the run they confirmed, while it is still read, or else the
 * suggested run. Shown as ticked boxes for them to keep or change, never sent without their click.
 */
export function startingSelection(countertop: RunCountertop): string[] {
  const decision = countertop.decision;
  if (decision?.decision === 'confirmed' && decision.read) {
    return decision.members.map((member) => member.item_id);
  }
  return (countertop.suggestion?.members ?? []).map((member) => member.item_id);
}

/**
 * Whether the ticked parts are exactly the suggested run, whatever order they were ticked in. Never
 * true when nothing is suggested: there is then no suggestion to confirm, only parts to tick.
 */
export function isTheSuggestion(countertop: RunCountertop, selected: readonly string[]): boolean {
  const suggested = (countertop.suggestion?.members ?? []).map((member) => member.item_id);
  return (
    suggested.length > 0 &&
    suggested.length === selected.length &&
    suggested.every((id) => selected.includes(id))
  );
}

/** What a person last said about a countertop's run, in a sentence. */
export function runDecisionLabel(countertop: RunCountertop): string {
  const decision = countertop.decision;
  if (!decision) return 'Not decided yet.';
  if (decision.decision === 'withdrawn') {
    return `Said not to be this countertop's run by ${decision.decided_by}.`;
  }
  const members = decision.members.map((member) => partLabel(member)).join(', ');
  const layout = decision.wall_config
    ? ` Wall layout: ${wallLayoutLabel(decision.wall_config)}, chosen by ${decision.decided_by}.`
    : ' Wall layout not recorded; reconfirm this run to choose it.';
  return `Confirmed by ${decision.decided_by}: ${members}, left to right.${layout}`;
}
