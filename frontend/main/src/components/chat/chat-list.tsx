import { useState } from 'react';
import { Plus } from 'lucide-react';

import { formatRoute } from '@/app/route';
import { SidebarMenuSub, SidebarMenuSubButton, SidebarMenuSubItem } from '@/components/ui/sidebar';
import type { ChatSummary } from '@/lib/chat-api';
import { chatTitle } from '@/lib/chat-messages';

/** `missing` (#1685): the drawing set itself was not found (404), not merely its list. */
export type ChatListState = { status: 'loading' } | { status: 'error'; message: string; missing?: boolean } | { status: 'ready'; chats: ChatSummary[] };

/** How many chats show before "Show all". */
export const CHATS_SHOWN = 10;

export interface ChatListProps {
  packageId: string;
  activeChatId: string | null;
  state: ChatListState;
  /** True while a new chat is being made. */
  creating: boolean;
  onNewChat: () => void;
  /** Chats whose first message was sent here: their title fades in when the server names them. */
  freshTitles?: ReadonlySet<string>;
}

/**
 * A drawing set's chats (#1464, #1471), shown in the app's one sidebar under the open set, the way
 * claude.ai shows a project's chats: "New chat" with a plus, then one line per chat (cut with "…"),
 * newest first, the open one highlighted, the first ten and then "Show all". Each chat is a real
 * link, so it can be opened in a new tab.
 */
export function ChatList({ packageId, activeChatId, state, creating, onNewChat, freshTitles }: ChatListProps) {
  const [showAll, setShowAll] = useState(false);
  const chats = state.status === 'ready' ? state.chats : [];
  // The open chat always shows, even when it is older than the first ten.
  const activeIndex = chats.findIndex((chat) => chat.id === activeChatId);
  const shown = showAll || activeIndex >= CHATS_SHOWN ? chats : chats.slice(0, CHATS_SHOWN);
  const muted = 'px-2 py-1 text-xs text-muted-foreground';

  return (
    <nav data-slot="chat-list" aria-label="Chats in this drawing set">
      <SidebarMenuSub className="gap-0.5">
        <SidebarMenuSubItem>
          <SidebarMenuSubButton asChild>
            <button type="button" onClick={onNewChat} disabled={creating} className="font-medium">
              <Plus aria-hidden="true" />
              <span>{creating ? 'Starting a chat…' : 'New chat'}</span>
            </button>
          </SidebarMenuSubButton>
        </SidebarMenuSubItem>
        {state.status === 'loading' && <li className={muted}>Loading chats…</li>}
        {state.status === 'error' && (
          <li className={muted} role="alert">
            Could not load the chats: {state.message}
          </li>
        )}
        {shown.map((chat) => {
          const active = chat.id === activeChatId;
          const title = chatTitle(chat);
          const fresh = Boolean(freshTitles?.has(chat.id) && chat.title);
          return (
            <SidebarMenuSubItem key={chat.id}>
              <SidebarMenuSubButton
                href={formatRoute({ page: 'chat', packageId, chatId: chat.id })}
                isActive={active}
                aria-current={active ? 'page' : undefined}
                title={title}
                className="transition-colors duration-150 data-[active=true]:font-medium"
              >
                {/* Keyed by the title, so a title the first message just set mounts afresh and fades in. */}
                <span
                  key={chat.title ?? ''}
                  data-fresh={fresh ? 'true' : undefined}
                  className={fresh ? 'motion-safe:animate-in motion-safe:fade-in-0 motion-safe:duration-500' : undefined}
                >
                  {title}
                </span>
              </SidebarMenuSubButton>
            </SidebarMenuSubItem>
          );
        })}
        {chats.length > CHATS_SHOWN && activeIndex < CHATS_SHOWN && (
          <SidebarMenuSubItem>
            <SidebarMenuSubButton asChild size="sm">
              <button type="button" aria-expanded={showAll} onClick={() => setShowAll((now) => !now)} className="text-muted-foreground">
                <span>{showAll ? 'Show fewer' : `Show all (${chats.length})`}</span>
              </button>
            </SidebarMenuSubButton>
          </SidebarMenuSubItem>
        )}
      </SidebarMenuSub>
    </nav>
  );
}
