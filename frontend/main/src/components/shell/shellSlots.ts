import { createContext } from 'react';

/**
 * The two places in the header a screen may fill: beside the title, and the actions on the right.
 * Elements rather than React state, so a screen portals its own controls into the header and keeps
 * owning their handlers — the header does not need to know what a review's sign-off does.
 */
export interface ShellSlots {
  title: HTMLElement | null;
  actions: HTMLElement | null;
}

export const ShellSlotsContext = createContext<ShellSlots>({ title: null, actions: null });
