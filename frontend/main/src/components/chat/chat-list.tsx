import { ListChecks, SquarePen } from 'lucide-react';

import { formatRoute } from '@/app/route';
import type { ChatSummary } from '@/lib/chat-api';
import { chatTitle, relativeTime } from '@/lib/chat-messages';
import { cn } from '@/lib/utils';

export type ChatListState = { status: 'loading' } | { status: 'error'; message: string } | { status: 'ready'; chats: ChatSummary[] };

export interface ChatListProps {
  packageId: string;
  activeChatId: string | null;
  state: ChatListState;
  /** True while a new chat is being made. */
  creating: boolean;
  onNewChat: () => void;
  /** Called when a chat is picked, after the link has moved the address (phones close their menu). */
  onPicked?: () => void;
  now?: Date;
}

/**
 * A drawing set's chats (#1464), newest first, like claude.ai's sidebar: New chat on top, then each
 * chat by its title and when it was last used. Each chat is a real link, so it can be opened in a
 * new tab. "Review list" goes to this set's review screen.
 */
export function ChatList({ packageId, activeChatId, state, creating, onNewChat, onPicked, now = new Date() }: ChatListProps) {
  return (
    <nav data-slot="chat-list" aria-label="Chats in this drawing set" className="flex min-h-0 flex-1 flex-col gap-1 p-2">
      <button
        type="button"
        onClick={onNewChat}
        disabled={creating}
        className="flex h-9 shrink-0 items-center gap-2 rounded-lg px-2.5 text-sm font-medium hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none disabled:opacity-50"
      >
        <SquarePen className="size-4" aria-hidden="true" />
        {creating ? 'Starting a chat…' : 'New chat'}
      </button>

      <h2 className="px-2.5 pt-3 pb-1 text-xs font-medium text-muted-foreground">Chats</h2>
      <div className="min-h-0 flex-1 overflow-y-auto">
        {state.status === 'loading' && <p className="px-2.5 py-1 text-xs text-muted-foreground">Loading chats…</p>}
        {state.status === 'error' && (
          <p className="px-2.5 py-1 text-xs text-muted-foreground" role="alert">
            Could not load the chats: {state.message}
          </p>
        )}
        {state.status === 'ready' && state.chats.length === 0 && <p className="px-2.5 py-1 text-xs text-muted-foreground">No chats yet.</p>}
        {state.status === 'ready' && state.chats.length > 0 && (
          <ul className="flex flex-col gap-0.5">
            {state.chats.map((chat) => {
              const active = chat.id === activeChatId;
              const used = chat.last_message_at ?? chat.created_at;
              return (
                <li key={chat.id}>
                  <a
                    href={formatRoute({ page: 'chat', packageId, chatId: chat.id })}
                    aria-current={active ? 'page' : undefined}
                    onClick={() => onPicked?.()}
                    className={cn(
                      'flex min-w-0 flex-col rounded-lg px-2.5 py-1.5 hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none',
                      active && 'bg-accent',
                    )}
                  >
                    <span className={cn('truncate text-sm', active && 'font-medium')} title={chatTitle(chat)}>{chatTitle(chat)}</span>
                    <time dateTime={used} title={new Date(used).toLocaleString()} className="text-xs text-muted-foreground">
                      {relativeTime(used, now)}
                    </time>
                  </a>
                </li>
              );
            })}
          </ul>
        )}
      </div>

      <a
        href={formatRoute({ page: 'review', packageId })}
        className="flex h-9 shrink-0 items-center gap-2 rounded-lg border-t px-2.5 pt-1 text-sm text-muted-foreground hover:bg-accent hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
      >
        <ListChecks className="size-4" aria-hidden="true" />
        Review list
      </a>
    </nav>
  );
}
