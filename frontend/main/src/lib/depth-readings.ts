/**
 * What the depth and sink reader read for each countertop, in words (#1295), kept free of React so it
 * can be tested.
 *
 * The server says, per countertop and per value, what was read and how code checked it, a value only
 * its drawn length checks (a person confirms it, through the same confirm path Measurements uses), a
 * value a person already confirmed, or why a value was not used. Nothing here decides which reading a
 * check uses, and nothing here is ever typed: a drawn number is confirmed, never filled in (GV's
 * rule, Anant 2026-10-12).
 */
import type { components } from '@/api/schema';

export type DepthReading = components['schemas']['DepthReadingOut'];
export type LinkedView = components['schemas']['LinkedViewOut'];
export type VendorSinkNote = components['schemas']['VendorSinkNoteOut'];
export type ViewKind = NonNullable<DepthReading['view']>;

/** The view a value was read on, as a sentence names it. */
const ON_VIEW: Record<ViewKind, string> = {
  plan: 'the plan view',
  section: 'the section',
  side: 'the side view',
  front: 'the front view',
  detail: 'the detail view',
};

/** The view-kind chip on the Measure page. */
export const VIEW_CHIP: Record<ViewKind, string> = {
  plan: 'Plan view',
  section: 'Section',
  side: 'Side view',
  front: 'Front view',
  detail: 'Detail view',
};

/** How code checked a number both readers agreed on, in the reviewer's words. */
const CHECKED: Record<NonNullable<DepthReading['checked_by']>, string> = {
  printed_text: 'two readers, confirmed by the printed text',
  chain: 'two readers, confirmed by a chain of dimensions that adds up to it',
  drawn_length: 'only the drawn length checks it',
};

function onView(reading: DepthReading): string {
  return reading.view ? ON_VIEW[reading.view] : 'the drawing';
}

function lower(text: string): string {
  return text.slice(0, 1).toLowerCase() + text.slice(1);
}

/** One value's line, after its label: what was read, or why it was not used. Never a verdict. */
export function readingSentence(reading: DepthReading): string {
  const value = reading.value ?? '';
  switch (reading.state) {
    case 'read':
      return `read from ${onView(reading)}: ${value} (${CHECKED[reading.checked_by ?? 'printed_text']})`;
    case 'to_confirm':
      return `read ${value} on ${onView(reading)} — please confirm (only the drawn length checks it)`;
    case 'confirmed':
      return `${value} from ${onView(reading)}, confirmed by a person`;
    case 'not_this':
      return `a person said the ${value} on ${onView(reading)} is not the ${lower(reading.label)}`;
    case 'held':
      return reading.value
        ? `read ${value} on ${onView(reading)}, not used: ${reading.reason ?? 'no reason was recorded.'}`
        : `not used: ${reading.reason ?? 'no reason was recorded.'}`;
    case 'not_read':
      return `not read: ${reading.reason ?? 'no reason was recorded.'}`;
  }
}

/** The lines grouped by countertop, in the server's order. */
export function byCountertop(readings: readonly DepthReading[]): [string, DepthReading[]][] {
  const groups = new Map<string, DepthReading[]>();
  for (const reading of readings) {
    const group = groups.get(reading.countertop) ?? [];
    group.push(reading);
    groups.set(reading.countertop, group);
  }
  return [...groups.entries()];
}

/** The vendor's sink note as the sink card shows it: a hint, never a value. */
export function sinkNoteSentence(note: VendorSinkNote): string {
  return `The vendor's drawing names this sink: “${note.text}” (page ${note.page_number}).`;
}

export const SINK_NOTE_HINT = "A hint only: type the sink's inside size from its spec sheet.";
