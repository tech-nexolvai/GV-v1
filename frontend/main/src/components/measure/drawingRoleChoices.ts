import type { components } from '../../api/schema';

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

/**
 * How many drawings still need a person: unconfirmed, and not decided by the upload either.
 *
 * A two-PDF package's one-drawing pages take the upload's side until somebody says otherwise
 * (admin, 2026-10-01, #795), so asking about them would be asking a question already answered.
 */
export function stillToConfirm(views: readonly DrawingView[]): number {
  return views.filter((view) => view.role === null && !view.upload_side).length;
}

