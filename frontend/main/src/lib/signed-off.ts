import { createContext, useContext } from 'react';

/**
 * Whether the open review is signed off (#1182). After sign-off nothing about it changes: the
 * server refuses every save, decision and check run with 409, so the screen does not offer them.
 * Provided once by the review page; every control that writes reads it.
 */
export const SignedOffContext = createContext(false);

/**
 * The states in which a review is closed to change, the server's own list (`app/lifecycle/closed.py`):
 * signed off, sent back, replaced by a newer revision, cancelled. The screen offers no write in any.
 */
const CLOSED_STATES: ReadonlySet<string> = new Set(['APPROVED', 'CHANGES_REQUESTED', 'SUPERSEDED', 'CANCELLED']);

export function isClosedState(state: string | null | undefined): boolean {
  return state != null && CLOSED_STATES.has(state);
}

export function useSignedOff(): boolean {
  return useContext(SignedOffContext);
}

/** Shown where a control that writes is turned off after sign-off (or once the review is closed). */
export const SIGNED_OFF_NOTE = 'Signed off: this review can no longer change. Start a new revision to change it.';

/** A save that is never sent: its refusal is shown where the saved answer would be. */
export async function refuseClosed(): Promise<never> {
  throw new Error(SIGNED_OFF_NOTE);
}
