import { useContext } from 'react';
import { createPortal } from 'react-dom';
import { ShellSlotsContext } from './shellSlots';

/** Renders its children on the right of the header, for as long as the screen is mounted. */
export function HeaderActions({ children }: { children: React.ReactNode }) {
  const { actions } = useContext(ShellSlotsContext);
  return actions ? createPortal(children, actions) : null;
}

/** Renders its children beside the header title (hidden under 1024px, where there is no room). */
export function HeaderTitleExtra({ children }: { children: React.ReactNode }) {
  const { title } = useContext(ShellSlotsContext);
  return title ? createPortal(children, title) : null;
}

/** Renders its children under the open drawing set in the sidebar (the set's chats, #1471). */
export function SidebarChats({ children }: { children: React.ReactNode }) {
  const { chats } = useContext(ShellSlotsContext);
  return chats ? createPortal(children, chats) : null;
}
