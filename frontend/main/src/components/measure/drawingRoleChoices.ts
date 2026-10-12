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

/** A heading's suggestion in words: one of the two sides, or a reference drawing. */
export function suggestionLabel(role: string | null | undefined): string | null {
  return role === 'reference' ? REFERENCE_LABEL : roleLabel(role);
}

/**
 * How many drawings still need a person: unconfirmed, and not decided by the upload either.
 *
 * A two-PDF package's one-drawing pages take the upload's side until somebody says otherwise
 * (admin, 2026-10-01, #795), so asking about them would be asking a question already answered.
 */
export function stillToConfirm(views: readonly DrawingView[]): number {
  return views.filter((view) => view.role === null && !view.upload_side).length;
}

/** For the Measurements step bar (#1061): every drawing, and how many are settled. */
export function roleCount(views: readonly DrawingView[]): StepCount {
  return countOf(views.length, stillToConfirm(views));
}

