import { PanelLeftClose, PanelLeftOpen, Plus } from 'lucide-react';

import { formatRoute } from '@/app/route';
import type { ChatSummary } from '@/lib/chat-api';
import { chatTitle } from '@/lib/chat-messages';
import { cn } from '@/lib/utils';

export type ChatListState = { status: 'loading' } | { status: 'error'; message: string } | { status: 'ready'; chats: ChatSummary[] };

export interface ChatListProps {
  packageId: string;
  activeChatId: string | null;
  state: ChatListState;
  /** True while a new chat is being made. */
  creating: boolean;
  onNewChat: () => void;
  /** Called when a chat is picked, after the link has moved the address (the phone drawer closes). */
  onPicked?: () => void;
  /** The column is folded to a slim rail (its toggle and New chat stay). */
  collapsed?: boolean;
  /** Fold or unfold the column; no toggle is shown without it (the phone drawer). */
  onToggle?: () => void;
  /** Chats whose first message was sent here: their title fades in when the server names them. */
  freshTitles?: ReadonlySet<string>;
}

const ROW = 'flex h-9 min-w-0 shrink-0 items-center gap-2.5 rounded-lg px-2 text-sm transition-colors duration-150 hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none';

/**
 * A drawing set's chats (#1464, #1471), newest first, like claude.ai's sidebar: a fold toggle, New
 * chat with a plus, then "Recents", one line per chat (cut with "…"), the open one highlighted. Each
 * chat is a real link, so it can be opened in a new tab.
 */
export function ChatList({ packageId, activeChatId, state, creating, onNewChat, onPicked, collapsed = false, onToggle, freshTitles }: ChatListProps) {
  return (
    <nav data-slot="chat-list" aria-label="Chats in this drawing set" className="flex min-h-0 w-[260px] flex-1 flex-col gap-0.5 p-2">
      {onToggle && (
        <button
          type="button"
          onClick={onToggle}
          aria-label={collapsed ? 'Show chats' : 'Hide chats'}
          aria-expanded={!collapsed}
          title={collapsed ? 'Show chats' : 'Hide chats'}
          className="mb-1 grid size-9 shrink-0 place-items-center rounded-lg text-muted-foreground transition-colors duration-150 hover:bg-accent hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
        >
          {collapsed ? <PanelLeftOpen className="size-[18px]" aria-hidden="true" /> : <PanelLeftClose className="size-[18px]" aria-hidden="true" />}
        </button>
      )}
      <button type="button" onClick={onNewChat} disabled={creating} aria-label="New chat" title="New chat" className={cn(ROW, 'font-medium disabled:opacity-50')}>
        <span className="grid size-[22px] shrink-0 place-items-center rounded-full bg-primary text-primary-foreground">
          <Plus className="size-3.5" aria-hidden="true" />
        </span>
        <span className={cn('truncate transition-opacity duration-150', collapsed && 'opacity-0')}>{creating ? 'Starting a chat…' : 'New chat'}</span>
      </button>

      <div
        data-slot="chat-recents"
        // Folded: out of sight, out of the tab order and out of the accessibility tree.
        hidden={collapsed || undefined}
        className="flex min-h-0 flex-1 flex-col motion-safe:animate-in motion-safe:fade-in-0 motion-safe:duration-200"
      >
        <h2 className="px-2 pt-4 pb-1.5 text-xs font-medium text-muted-foreground">Recents</h2>
        <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain">
          {state.status === 'loading' && <p className="px-2 py-1 text-xs text-muted-foreground">Loading chats…</p>}
          {state.status === 'error' && (
            <p className="px-2 py-1 text-xs text-muted-foreground" role="alert">
              Could not load the chats: {state.message}
            </p>
          )}
          {state.status === 'ready' && state.chats.length === 0 && <p className="px-2 py-1 text-xs text-muted-foreground">No chats yet.</p>}
          {state.status === 'ready' && state.chats.length > 0 && (
            <ul className="flex flex-col gap-px">
              {state.chats.map((chat) => {
                const active = chat.id === activeChatId;
                const title = chatTitle(chat);
                return (
                  <li key={chat.id}>
                    <a
                      href={formatRoute({ page: 'chat', packageId, chatId: chat.id })}
                      aria-current={active ? 'page' : undefined}
                      onClick={() => onPicked?.()}
                      title={title}
                      className={cn(
                        'flex h-8 min-w-0 items-center rounded-lg px-2 text-[13.5px] text-foreground/85 transition-colors duration-150 hover:bg-accent hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none',
                        active && 'bg-accent font-medium text-foreground',
                      )}
                    >
                      {/* Keyed by the title, so a title the first message just set mounts afresh and fades in. */}
                      <span
                        key={chat.title ?? ''}
                        data-fresh={(freshTitles?.has(chat.id) && chat.title) ? 'true' : undefined}
                        className={cn('truncate', freshTitles?.has(chat.id) && chat.title && 'motion-safe:animate-in motion-safe:fade-in-0 motion-safe:duration-500')}
                      >
                        {title}
                      </span>
                    </a>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      </div>
    </nav>
  );
}
