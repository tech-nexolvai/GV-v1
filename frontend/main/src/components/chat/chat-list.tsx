import { useState } from 'react';
import { Check, MessageCircle, Plus, ScanSearch } from 'lucide-react';

import { formatRoute } from '@/app/route';
import { GVSpark } from '@/components/chat/chat-motion';
import { SidebarMenuSub, SidebarMenuSubButton, SidebarMenuSubItem } from '@/components/ui/sidebar';
import { cn } from '@/lib/utils';
import type { ChatSummary } from '@/lib/chat-api';
import { checkMarks, chatListWords, chatRunWords, chatTitle, NO_CHECK_MARKS, type ChatRunLive } from '@/lib/chat-messages';

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
  /** #1727: the open chat's running full check, from its live card (newer than the list's counts). */
  live?: ChatRunLive | null;
}


/**
 * A drawing set's chats (#1464, #1471), shown in the app's one sidebar under the open set, the way
 * claude.ai shows a project's chats: "New chat" with a plus, then one line per chat (cut with "…"),
 * newest first, the open one highlighted, the first ten and then "Show all". Each chat is a real
 * link, so it can be opened in a new tab. #1712: a full check reads "Full check" with its time, a
 * question its own words with its day, each with a small icon; no counts (each number has one place,
 * on the answer). #1765: a full check seen running here that has ended gets a small grey done mark.
 */
export function ChatList({ packageId, activeChatId, state, creating, onNewChat, freshTitles, live = null }: ChatListProps) {
  const [showAll, setShowAll] = useState(false);
  const chats = state.status === 'ready' ? state.chats : [];
  // #1765: which full checks ended while this page watched (kept until the page is reloaded).
  const [marks, setMarks] = useState(NO_CHECK_MARKS);
  if (state.status === 'ready') {
    const next = checkMarks(marks, chats, live);
    if (next !== marks) setMarks(next);
  }
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
          const words = chatListWords(chat);
          const Icon = words.kind === 'check' ? ScanSearch : MessageCircle;
          const fresh = Boolean(freshTitles?.has(chat.id) && chat.title);
          const run = chatRunWords(chat, live);
          const done = !run.running && marks.done.has(chat.id);
          const runWords = run.running ? `, running${run.count ? `: ${run.count.replace('/', ' of ')} drawings compared` : ''}` : done ? ', finished' : '';
          return (
            <SidebarMenuSubItem key={chat.id}>
              <SidebarMenuSubButton
                href={formatRoute({ page: 'chat', packageId, chatId: chat.id })}
                isActive={active}
                aria-current={active ? 'page' : undefined}
                // Every full check reads "Full check": its time tells them apart, for screen readers too.
                aria-label={words.kind === 'check' && words.when ? `Full check, ${words.when}${runWords}` : runWords ? `${words.label}${runWords}` : undefined}
                title={title}
                className="transition-colors duration-150 data-[active=true]:font-medium"
              >
                <Icon aria-hidden="true" />
                {/* Keyed by the title, so a title the first message just set mounts afresh and fades in. */}
                <span
                  key={chat.title ?? ''}
                  data-fresh={fresh ? 'true' : undefined}
                  data-kind={words.kind}
                  className={cn('min-w-0 flex-1 truncate', fresh && 'motion-safe:animate-in motion-safe:fade-in-0 motion-safe:duration-500')}
                >
                  {words.label}
                </span>
                {run.running ? (
                  <span data-slot="chat-list-running" aria-hidden="true" className="ml-auto inline-flex shrink-0 items-center gap-1.5 text-xs text-muted-foreground">
                    {run.count && <span className="num">{run.count}</span>}
                    <GVSpark state="working" size={13} />
                  </span>
                ) : done ? (
                  <span data-slot="chat-list-done" aria-hidden="true" className="ml-auto inline-flex shrink-0 items-center gap-1 text-xs text-muted-foreground">
                    <Check className="size-3.5" strokeWidth={2.25} />
                    {words.when && <span className="num">{words.when}</span>}
                  </span>
                ) : (
                  words.when && <span data-slot="chat-list-when" aria-hidden="true" className="num ml-auto shrink-0 text-xs text-muted-foreground">{words.when}</span>
                )}
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
