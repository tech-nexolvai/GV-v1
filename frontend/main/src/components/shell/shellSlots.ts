import { createContext } from 'react';

/**
 * The places in the frame a screen may fill: beside the header's title, the header's actions on the
 * right, and (on a drawing set's chats, #1471) the space under the open set in the sidebar, where
 * the chat page lists that set's chats, so there is one sidebar, as on claude.ai.
 * Elements rather than React state, so a screen portals its own controls into the header and keeps
 * owning their handlers — the header does not need to know what a review's sign-off does.
 */
export interface ShellSlots {
  title: HTMLElement | null;
  actions: HTMLElement | null;
  chats?: HTMLElement | null;
}

export const ShellSlotsContext = createContext<ShellSlots>({ title: null, actions: null, chats: null });
