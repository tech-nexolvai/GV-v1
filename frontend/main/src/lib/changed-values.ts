/**
 * One "changed value" line from the server, split into the columns a reviewer compares.
 *
 * The API sends each override as one plain-English line written by `rules/overrides.py`
 * (`Override.explain`):
 *
 *   `name = 25 1/4 in (PROJECT, typed on the form, set by reviewer); overrides 25 in (GLOBAL, company standard)`
 *
 * This reads that shape back into setting / GV standard / this project / source. A line in any other
 * shape is kept whole (`raw`) rather than guessed at: the sentence is the record, the table is only a
 * way of reading it.
 */
import type { components } from '@/api/schema';

type ChangedValues = components['schemas']['ChangedValuesOut'];

export type ChangedValueRow =
  | { kind: 'parsed'; setting: string; standard: string | null; project: string; source: string; raw: string }
  | { kind: 'raw'; raw: string };

const HEAD = /^(.+?) = (.+?) \(([A-Z_]+), (.*), set by (.+?)\); overrides (.+)$/;
const DISPLACED = /(.+?) \(([A-Z_]+), ([^)]*)\)(?:, |$)/g;

export function parseChangedValue(line: string): ChangedValueRow {
  const head = HEAD.exec(line.trim());
  if (!head) return { kind: 'raw', raw: line };
  const [, setting, value, , sourceText, setBy, rest] = head;
  let standard: string | null = null;
  for (const match of rest.matchAll(DISPLACED)) {
    if (match[2] === 'GLOBAL') {
      standard = inches(match[1]);
      break;
    }
  }
  return { kind: 'parsed', setting: setting.trim(), standard, project: inches(value), source: `${sourceText} · ${setBy}`, raw: line };
}

/** `25 1/4 in` → `25 1/4"`; anything else unchanged. */
function inches(value: string): string {
  return value.trim().replace(/ in$/, '"');
}

/** The badge's words: what differs first, then what is missing; never a count that did not load. */
export function changedValuesSummary(state: 'loading' | 'error' | 'ready', value: ChangedValues | null): string {
  if (state === 'loading') return 'Project values…';
  if (state === 'error' || value === null || value.message !== null) return 'Project values';
  const differ = value.company_standards_displaced.length;
  const missing = value.outstanding.length;
  if (differ > 0) return `${differ} project ${differ === 1 ? 'value differs' : 'values differ'}`;
  if (missing > 0) return `${missing} ${missing === 1 ? 'value' : 'values'} not set`;
  return 'GV standard values';
}
