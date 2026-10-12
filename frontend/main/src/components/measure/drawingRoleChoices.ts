import type { components } from '../../api/schema';
import { countOf, type StepCount } from '../../lib/measure-steps';

/** One drawing on a sheet, as the API lists it: its page, its printed label, its role if confirmed. */
export type DrawingView = components['schemas']['ViewOut'];

export type DrawingRole = 'arch' | 'shop';

export const DRAWING_ROLES: readonly DrawingRole[] = ['arch', 'shop'];

export const ROLE_LABEL: Record<DrawingRole, string> = {
  arch: "Architect's drawing",
  shop: "Vendor's drawing",
};

export function roleLabel(role: string | null | undefined): string | null {
  return role === 'arch' || role === 'shop' ? ROLE_LABEL[role] : null;
}

/** What a sheet's heading may suggest, beyond the two sides (#1193): a brand prototype is shown as a
 * reference drawing, which is never read or compared. Never a choice a person makes here. */
export const REFERENCE_LABEL = 'a reference drawing (brand prototype), never checked';

/** The line a reference drawing shows instead of the two answers (#1193 review). */
export const REFERENCE_NOTHING_TO_DO = 'Reference drawing (brand prototype): nothing to do';

/** A heading's suggestion in words: one of the two sides, or a reference drawing. */
export function suggestionLabel(role: string | null | undefined): string | null {
  return role === 'reference' ? REFERENCE_LABEL : roleLabel(role);
}

/**
 * Whether an answer on this drawing re-labels a reference drawing (#1193 review): its heading names a
 * brand prototype, whether or not a person already re-labelled it. Sent with every answer on it, so
 * a re-label can be corrected later; the API refuses an answer on such a drawing without it.
 */
export function relabelsReference(view: DrawingView): boolean {
  return view.suggested_role === 'reference';
}

/** A drawing nobody confirmed whose heading names a reference drawing: nothing to confirm. */
export function isReference(view: DrawingView): boolean {
  return view.role === null && view.suggested_role === 'reference';
}

/**
 * How many drawings still need a person: unconfirmed, and not decided by the upload either.
 *
 * A two-PDF package's one-drawing pages take the upload's side until somebody says otherwise
 * (admin, 2026-10-01, #795), so asking about them would be asking a question already answered.
 */
export function stillToConfirm(views: readonly DrawingView[]): number {
  // A reference drawing (a brand prototype, #1193) is never checked: nothing to confirm on it.
  return views.filter((view) => view.role === null && !view.upload_side && !isReference(view)).length;
}

/** For the Measurements step bar (#1061): every drawing, and how many are settled. */
export function roleCount(views: readonly DrawingView[]): StepCount {
  return countOf(views.length, stillToConfirm(views));
}

