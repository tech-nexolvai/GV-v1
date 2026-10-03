import type { components } from '../../api/schema';
import { sourceToSend, type SettingSource } from './settingSources.js';

/**
 * Where the architect's drawing states a setting, as `GET .../required-inputs` sends it (#866): an
 * id to send back, a page, which upload, and whether a crop exists. **Never the number.** The
 * reviewer types what they see, and the server saves it only if it matches the drawing.
 */
export type SettingPointer = components['schemas']['SettingPointerOut'];

/** A setting as the form holds it: enough to say where its value came from. */
export interface CitableSetting {
  name: string;
  scope: string;
  sources?: SettingSource[];
  found?: SettingPointer | null;
}

/** One setting on the wire, as `POST .../measurements` takes it. */
export interface SettingEntry {
  name: string;
  value: string;
  scope: 'project' | 'run';
  source?: string;
  reference?: string;
  citation?: string;
}

/**
 * The passage a setting's value is being typed from, or `null` when it is typed some other way.
 *
 * `null` when nothing was found, and when the reviewer chose to enter the value another way: the
 * passage is the app's suggestion of where to look, and a person may know better.
 */
export function citingPointer(
  setting: CitableSetting,
  declined: Record<string, boolean>,
): SettingPointer | null {
  return setting.found && !declined[setting.name] ? setting.found : null;
}

/** Which file a page belongs to, in the words a reviewer uses. */
export function uploadLabel(kind: string): string {
  if (kind === 'architectural') return "architect's file";
  if (kind === 'shop') return "vendor's file";
  return `${kind.replace(/_/g, ' ')} file`;
}

/**
 * What to send for one setting.
 *
 * **Typed from the passage, it sends the citation and nothing about the source.** The server reads
 * the passage, refuses a number that differs, and writes the source and the reference itself, so
 * the reviewer's own choices would only be something for it to refuse. Typed any other way, it is
 * sent exactly as before #866: the source the reviewer chose, and their reference if they gave one.
 */
export function settingEntry(
  setting: CitableSetting,
  typed: string,
  answers: { declined: Record<string, boolean>; source?: string; reference?: string },
): SettingEntry {
  const value = typed.trim();
  const scope = setting.scope === 'run' ? 'run' : 'project';
  const pointer = citingPointer(setting, answers.declined);
  if (pointer) return { name: setting.name, value, scope, citation: pointer.proposal_id };
  const source = sourceToSend(setting, answers.source);
  const reference = (answers.reference ?? '').trim();
  return {
    name: setting.name,
    value,
    scope,
    ...(source ? { source } : {}),
    ...(reference ? { reference } : {}),
  };
}
