/**
 * The "Needs" lines of a check waiting on a value (#1181), kept free of React so they can be tested.
 *
 * The server says exactly what a waiting check could not run without (`missing_inputs`): a setting
 * for this job, the project's sink, a number drawn on the vendor's drawing, or (for the cabinet
 * checks, #1205) the reviewer's own entry on Measurements: a list of cabinets, each cabinet's type,
 * the site width. Only the first two may be typed here: a drawn number is read, never filled in
 * (GV's rule, Anant 2026-10-12), so its line says the check needs the drawing and has no box; an
 * entry on Measurements says where it is entered, and in what order. Nothing here decides which values are missing or fills
 * one in, and only a new check run uses a value.
 */
import type { components } from '@/api/schema';

export type MissingInput = components['schemas']['MissingInputOut'];

/** A setting GV's default now answers: the reviewer may still set this job's own value over it. */
export function overridable(input: MissingInput): boolean {
  return input.askable && input.scope === 'project' && input.saved?.layer === 'company';
}

/** The values that get a box: settings not yet saved, and settings GV's default answers. */
export function typeable(inputs: readonly MissingInput[]): MissingInput[] {
  return inputs.filter((input) => input.askable && input.scope === 'project' && (input.saved == null || overridable(input)));
}

/** The sink values still missing: they open the sink entry instead of a box of their own. */
export function sinkNeeded(inputs: readonly MissingInput[]): MissingInput[] {
  return inputs.filter((input) => input.saved == null && input.scope === 'sink');
}

/** Drawn numbers nobody read: listed so the reviewer knows why the check waits; never typed. */
export function drawingNeeded(inputs: readonly MissingInput[]): MissingInput[] {
  return inputs.filter((input) => input.scope === 'drawing' && input.saved == null);
}

/** True when a value has been saved since this result was recorded: only a new run uses it. */
export function savedSinceResult(inputs: readonly MissingInput[]): boolean {
  return inputs.some((input) => input.saved != null);
}

/** Where a saved value is from, in words: GV's default, the sink card, this job or this package. */
export function savedFrom(input: MissingInput): string {
  const layer = input.saved?.layer;
  if (input.scope === 'measurements') return 'entered on Measurements';
  if (layer === 'company') return 'GV default';
  if (layer === 'run') return 'typed for this package';
  return input.scope === 'sink' ? 'from the sink card' : 'this job';
}

/** The source a setting is recorded with: its only one, or the one the reviewer chose. Never guessed. */
export function sourceFor(input: MissingInput, chosen: Readonly<Record<string, string>>): string | null {
  const sources = input.sources ?? [];
  if (sources.length === 1) return sources[0];
  const picked = chosen[input.key];
  return picked && sources.includes(picked) ? picked : null;
}

export interface NeedsEntry {
  parameters: { name: string; value: string; scope: 'project'; source: string; reference?: string }[];
}

/**
 * What one Save sends to the existing values endpoint, or why Save cannot be pressed yet, in the
 * words shown beside it. Each typed setting goes as a value for this job, with its source and the
 * note; a value goes as typed (with its unit) and the server parses it. A drawn number is never sent.
 */
export function saveState(
  inputs: readonly MissingInput[],
  drafts: Readonly<Record<string, string>>,
  note: string,
  chosen: Readonly<Record<string, string>> = {},
): { entry: NeedsEntry | null; blocked: string | null } {
  const reference = note.trim() || undefined;
  const parameters: NeedsEntry['parameters'] = [];
  for (const input of typeable(inputs)) {
    const value = (drafts[input.key] ?? '').trim();
    if (!value) continue;
    const source = sourceFor(input, chosen);
    if (source === null) return { entry: null, blocked: `Choose where ${input.label.toLowerCase()} came from.` };
    parameters.push({ name: input.key, value, scope: 'project', source, ...(reference ? { reference } : {}) });
  }
  if (parameters.length === 0) return { entry: null, blocked: 'Type a value, with its unit, to save.' };
  return { entry: { parameters }, blocked: null };
}
