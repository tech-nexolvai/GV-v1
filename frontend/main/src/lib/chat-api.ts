/**
 * The chat-first assistant's API (#1464): a drawing set's chats, their saved messages, and a new
 * message streamed back as server-sent events.
 *
 * Typed by hand from the agreed contract (`chat-first/CONTRACT.md`, "HTTP API" and "SSE events")
 * because the routes are not in the generated schema yet, and an SSE stream's events never are
 * (FastAPI describes it only as `text/event-stream`). When the backend lands, check these against
 * `schema.d.ts`; the screen only reads the fields below.
 *
 * Optional fields are typed `?: T | null`: the server may leave a field out or send it as null, and
 * both mean "not there".
 */

import { ApiError, type ErrorEnvelope } from '@/api/client';
import { parseSseFrames, type SseFrame } from '@/api/sse';

// ── Types: the contract's shapes ────────────────────────────

/** How hard the model works on a message, lowest first. */
export type ChatEffort = 'low' | 'medium' | 'high' | 'xhigh' | 'max';

/** One model the composer offers (`GET /assistant/models`). */
export interface ChatModel {
  /** The API model id, sent back with each message ("claude-opus-5-5"). */
  id: string;
  /** In words, for the menu and the footer ("Claude Opus 5.5"). */
  label: string;
  /** The model the composer starts on. */
  default: boolean;
  efforts: ChatEffort[];
  default_effort: ChatEffort;
  /** `always`: it always thinks (the switch is locked on). `optional`: the reviewer may turn it off. */
  thinking: 'always' | 'optional';
  /** With thinking off, the highest effort allowed; null when there is no limit (or it always thinks). */
  thinking_off_max_effort: ChatEffort | null;
}

export interface ChatModels {
  /** False when no model can answer on this server; `reason` says why in plain words. */
  available: boolean;
  reason?: string | null;
  provider: 'openrouter' | 'anthropic';
  models: ChatModel[];
  /** The most one message may cost, in dollars ("5.00"). */
  message_cap_usd: string;
  /** The most one drawing set's chats may cost together, and what they have cost so far. */
  package_cap_usd: string;
  package_spent_usd: string;
}

/** One chat in a drawing set's list (`GET /packages/{id}/chats`), newest first. */
export interface ChatSummary {
  id: string;
  /** Set by the first message (its first 60 characters); null until then. */
  title: string | null;
  created_at: string;
  last_message_at: string | null;
  /** How many questions it holds. */
  turns: number;
}

export interface ChatList {
  /** Only chats with at least one message: a chat is made on its first message. */
  chats: ChatSummary[];
  /** The drawing set's name, for a new chat's greeting. */
  package_name?: string | null;
}

/** `POST /packages/{id}/chats`: a new empty chat. */
export interface CreatedChat {
  id: string;
  title: null;
  created_at: string;
}

/** One chat with every saved message (`GET /chats/{id}`). */
export interface Chat {
  id: string;
  package_id: string;
  /** The drawing set's name, for "What do you want to know about …?". */
  package_name: string;
  title: string | null;
  created_at: string;
  messages: ChatMessage[];
  /** True while a message of this chat is being answered (its answer is not stored yet). */
  running?: boolean | null;
  /** The answer being written while `running`: followed live with `followChatMessage`. */
  running_message_id?: string | null;
}

/** Why a message stopped before it finished; null or absent when it finished. */
export type ChatStopped = 'cost_cap' | 'cancelled' | 'error' | 'turn_limit';

/** A message as stored and as returned. */
export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant';
  created_at: string;
  /** The user's question; for the assistant, its final markdown answer. */
  text: string;
  /** The assistant's: what was used for it. */
  model?: string | null;
  effort?: ChatEffort | null;
  thinking?: boolean | null;
  /** The assistant's: thinking, words and tool steps in the order they happened. */
  blocks?: ChatBlock[] | null;
  files?: ChatFile[] | null;
  /** What the message cost, in dollars ("1.72"). */
  cost_usd?: string | null;
  stopped?: ChatStopped | null;
  /** Plain words about a failure (`stopped: "error"`). */
  error?: string | null;
  /** The per-answer cap and the drawing set's AI budget this answer ran under ("5.00", "40.00"). */
  message_cap_usd?: string | null;
  package_cap_usd?: string | null;
  /** With `stopped: "cost_cap"`: which one stopped it. */
  cap_hit?: 'message' | 'package' | null;
}

export type ChatBlock =
  | { type: 'thinking'; text: string; seconds: number | null }
  | { type: 'text'; text: string }
  | { type: 'step'; step: ChatStep };

export type ChatTool =
  | 'bash_tool'
  | 'view'
  | 'create_file'
  | 'str_replace'
  | 'present_files'
  | 'TaskCreate'
  | 'TaskUpdate'
  | 'Agent'
  | 'review_results';

/** One tool the assistant used. `step_start` sends it without its output; `step_end` sends it whole. */
export interface ChatStep {
  id: string;
  tool: ChatTool;
  /** The model's own description of the step ("Render page 3"). */
  label: string;
  command?: string | null;
  path?: string | null;
  output?: string | null;
  /** Null while it runs. */
  ok: boolean | null;
  seconds?: number | null;
  /** A picture it viewed, served at `/chats/{chat}/pictures/{picture_id}`. */
  picture_id?: string | null;
  /** TaskCreate / TaskUpdate: the whole task list after it. */
  tasks?: ChatTask[] | null;
  /** Agent: the helpers it ran, each with its own steps and report. */
  agents?: ChatAgent[] | null;
}

export interface ChatTask {
  id: string;
  subject: string;
  /** Shown while it is in progress ("Checking the sinks"). */
  active_form?: string | null;
  status: 'pending' | 'in_progress' | 'completed';
}

export interface ChatAgent {
  id: string;
  description: string;
  /** The model it ran on ("claude-sonnet-5-5"). */
  model: string;
  /** Null while it runs. */
  ok: boolean | null;
  report: string | null;
  steps: ChatStep[];
  stopped?: 'cost_cap' | 'turn_limit' | 'cancelled' | 'error' | null;
}

/** A file the assistant made and handed over, downloaded from `url`. */
export interface ChatFile {
  id: string;
  name: string;
  /** Bytes. */
  size: number;
  mime: string;
  url: string;
}

/** `POST /chats/{id}/messages`. */
export interface SendMessageBody {
  /** 1 to 8000 characters. */
  text: string;
  model: string;
  effort: ChatEffort;
  thinking: boolean;
}

/** The stream's events, in the contract's words. `index` is the block's place in `blocks`. */
export type ChatEvent =
  | { event: 'message_start'; data: { message_id: string; model: string; effort: ChatEffort; thinking: boolean } }
  | { event: 'thinking_start'; data: { index: number } }
  | { event: 'thinking_delta'; data: { index: number; text: string } }
  | { event: 'thinking_end'; data: { index: number; seconds: number } }
  | { event: 'text_delta'; data: { index: number; text: string } }
  | { event: 'step_start'; data: { index: number; step: ChatStep } }
  | { event: 'step_end'; data: { index: number; step: ChatStep } }
  | { event: 'tasks'; data: { tasks: ChatTask[] } }
  | { event: 'agent_start'; data: { step_index: number; agent: Pick<ChatAgent, 'id' | 'description' | 'model'> } }
  | { event: 'agent_step'; data: { step_index: number; agent_id: string; step: ChatStep } }
  | { event: 'agent_end'; data: { step_index: number; agent: Omit<ChatAgent, 'steps'> } }
  | { event: 'file'; data: { file: ChatFile } }
  | { event: 'usage'; data: { cost_usd: string } }
  | { event: 'message_end'; data: { message: ChatMessage } }
  | { event: 'error'; data: { message: string } };

export type ChatEventName = ChatEvent['event'];

const EVENT_NAMES: ReadonlySet<string> = new Set<ChatEventName>([
  'message_start', 'thinking_start', 'thinking_delta', 'thinking_end', 'text_delta', 'step_start', 'step_end',
  'tasks', 'agent_start', 'agent_step', 'agent_end', 'file', 'usage', 'message_end', 'error',
]);

/**
 * A parsed SSE frame as a contract event, or null for anything else (a frame this screen does not
 * know, or one with no data object). Unknown events are skipped, so a newer server never breaks it.
 */
export function chatEventOf(frame: SseFrame): ChatEvent | null {
  if (!EVENT_NAMES.has(frame.event) || frame.data === null || typeof frame.data !== 'object') return null;
  return { event: frame.event, data: frame.data } as ChatEvent;
}

// ── Paths ───────────────────────────────────────────────────

/** The same base the rest of the app uses (`/api/v1`, proxied to the API in development). */
const BASE = (import.meta.env.VITE_API_BASE_URL ?? '/api/v1').replace(/\/$/, '');

const id = (value: string) => encodeURIComponent(value);

/** Every chat route in one place, so a path change from the backend is one edit here. */
export const chatPaths = {
  /** With the drawing set, so `package_spent_usd` is that set's chat spend (else always 0.00). */
  models: (packageId?: string) => `${BASE}/assistant/models${packageId ? `?package_id=${id(packageId)}` : ''}`,
  chats: (packageId: string) => `${BASE}/packages/${id(packageId)}/chats`,
  chat: (chatId: string) => `${BASE}/chats/${id(chatId)}`,
  messages: (chatId: string) => `${BASE}/chats/${id(chatId)}/messages`,
  /** A message's events: the running one's so far then live, or a finished one's end at once. */
  events: (chatId: string, messageId: string) => `${BASE}/chats/${id(chatId)}/messages/${id(messageId)}/events`,
  cancel: (chatId: string) => `${BASE}/chats/${id(chatId)}/cancel`,
  file: (chatId: string, fileId: string) => `${BASE}/chats/${id(chatId)}/files/${id(fileId)}`,
  picture: (chatId: string, pictureId: string) => `${BASE}/chats/${id(chatId)}/pictures/${id(pictureId)}`,
};

// ── Requests ────────────────────────────────────────────────

/** A non-2xx answer as an ApiError, from the server's envelope when it sent one. */
async function failure(response: Response): Promise<ApiError> {
  let envelope: ErrorEnvelope;
  try {
    envelope = (await response.json()) as ErrorEnvelope;
    if (typeof envelope?.message !== 'string') throw new Error('no envelope');
  } catch {
    envelope = {
      error: 'unreadable_response',
      message: `The server returned ${response.status} and a body this client could not parse.`,
      request_id: response.headers.get('x-request-id') ?? 'unknown',
    };
  }
  return new ApiError(response.status, envelope);
}

async function json<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    ...init,
    headers: { Accept: 'application/json', ...(init?.body ? { 'Content-Type': 'application/json' } : {}), ...(init?.headers ?? {}) },
  });
  if (!response.ok) throw await failure(response);
  return (await response.json()) as T;
}

export function getChatModels(packageId?: string): Promise<ChatModels> {
  return json<ChatModels>(chatPaths.models(packageId));
}

export function listChats(packageId: string): Promise<ChatList> {
  return json<ChatList>(chatPaths.chats(packageId));
}

export function createChat(packageId: string): Promise<CreatedChat> {
  return json<CreatedChat>(chatPaths.chats(packageId), { method: 'POST', body: '{}' });
}

export function getChat(chatId: string): Promise<Chat> {
  return json<Chat>(chatPaths.chat(chatId));
}

/** Stop the running message between steps (its sandbox command and helpers too). */
export function cancelChat(chatId: string): Promise<{ cancelled: boolean }> {
  return json<{ cancelled: boolean }>(chatPaths.cancel(chatId), { method: 'POST', body: '{}' });
}

/**
 * Send a message and read its events as they arrive, calling `onEvent` for each one in order.
 * Resolves with the stored assistant message from `message_end`.
 *
 * A refused request (409 while another message runs, 422 for a bad body) rejects with its ApiError
 * before any event. A stream that ends without `message_end`, or a frame that is not JSON, rejects
 * with plain words: a cut-off answer must not read as a finished one. Aborting `signal` rejects with
 * the browser's AbortError, which the caller tells apart from a failure.
 */
export async function streamChatMessage(
  chatId: string,
  body: SendMessageBody,
  onEvent: (event: ChatEvent) => void,
  signal?: AbortSignal,
): Promise<ChatMessage> {
  const response = await fetch(chatPaths.messages(chatId), {
    method: 'POST',
    headers: { Accept: 'text/event-stream', 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal,
  });
  return readChatStream(response, onEvent);
}

/**
 * Follow a message (#1479), like claude.ai when you come back to a chat that is still answering:
 * the running message's events so far (streamed words arrive merged), then each new one live, until
 * `message_end`; for a message already finished, `message_end` at once. Same events and same
 * failures as `streamChatMessage`. Aborting `signal` only stops reading: the message goes on.
 */
export async function followChatMessage(
  chatId: string,
  messageId: string,
  onEvent: (event: ChatEvent) => void,
  signal?: AbortSignal,
): Promise<ChatMessage> {
  const response = await fetch(chatPaths.events(chatId, messageId), { headers: { Accept: 'text/event-stream' }, signal });
  return readChatStream(response, onEvent);
}

async function readChatStream(response: Response, onEvent: (event: ChatEvent) => void): Promise<ChatMessage> {
  const requestId = response.headers.get('x-request-id') ?? 'unknown';
  if (!response.ok || !response.body) throw await failure(response);

  const unreadable = () =>
    new ApiError(502, { error: 'unreadable_stream', message: 'The answer could not be read. Reload the chat to see what was saved.', request_id: requestId });
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffered = '';
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      let frames: SseFrame[];
      try {
        const parsed = parseSseFrames(buffered + decoder.decode(value, { stream: true }));
        frames = parsed.frames;
        buffered = parsed.rest;
      } catch {
        // A frame that is not JSON: said in plain words, never the parser's own text.
        throw unreadable();
      }
      for (const frame of frames) {
        const event = chatEventOf(frame);
        if (!event) continue;
        onEvent(event);
        if (event.event === 'message_end') {
          if (!event.data.message || typeof event.data.message.id !== 'string') throw unreadable();
          return event.data.message;
        }
      }
    }
  } finally {
    // The stored message, or a failure, is the last thing that matters: stop reading either way.
    reader.cancel().catch(() => {});
  }
  throw new ApiError(502, {
    error: 'incomplete_stream',
    message: 'The answer stopped before it was complete. Reload the chat to see what was saved.',
    request_id: requestId,
  });
}

/**
 * A failure in plain words. The server's own messages already are; a refused send, a lost
 * connection or an unreadable reply are said here instead, never as a parser's or browser's text.
 */
export function chatFailureWords(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 409) return 'Another message is still running in this chat. Wait for it to finish, or press Stop.';
    if (error.status === 422) return 'That message could not be sent. Check it is not too long and try again.';
    if (error.status === 404 && (error.code === 'unreadable_response' || !error.message)) return 'This was not found on the server.';
    if (error.code === 'unreadable_response' || !error.message) return 'The assistant could not answer right now. Try again.';
    return error.message;
  }
  // Only a request that never reached the server (fetch's TypeError) is a connection problem.
  if (error instanceof TypeError) return 'The assistant could not be reached. Check the connection and try again.';
  return 'Something went wrong. Try again.';
}

/** True for the browser's own "stopped on purpose" (an aborted fetch). */
export function isAbort(error: unknown): boolean {
  return error instanceof DOMException ? error.name === 'AbortError' : error instanceof Error && error.name === 'AbortError';
}

/** The calls the chat page makes. The app uses these; tests pass fakes. */
export interface ChatApi {
  models: typeof getChatModels;
  list: typeof listChats;
  create: typeof createChat;
  get: typeof getChat;
  send: typeof streamChatMessage;
  follow: typeof followChatMessage;
  cancel: typeof cancelChat;
}

export const LIVE_CHAT_API: ChatApi = {
  models: getChatModels,
  list: listChats,
  create: createChat,
  get: getChat,
  send: streamChatMessage,
  follow: followChatMessage,
  cancel: cancelChat,
};
