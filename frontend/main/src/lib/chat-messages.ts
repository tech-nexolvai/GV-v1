/**
 * A chat message as the screen shows it (#1464), built from the stream's events or from the saved
 * message, kept free of React so it can be tested alone.
 *
 * The stream sends each block by its place in `blocks` (`index`), so thinking, words and tool steps
 * stay in the order they happened, exactly as a reload shows them. `message_end` carries the stored
 * message, which then replaces what was built live: a reload and a live answer end up identical.
 */

import type { ChatAgent, ChatBlock, ChatEffort, ChatEvent, ChatFile, ChatMessage, ChatModel, ChatStep, ChatStopped, ChatSummary, ChatTask } from '@/lib/chat-api';
import { EFFORT_WORDS } from '@/lib/chat-settings';

/** A helper agent while its Agent step runs: `running` until its `agent_end`. */
export interface ViewAgent extends ChatAgent {
  running?: boolean;
}

export interface ViewStep extends ChatStep {
  agents?: ViewAgent[] | null;
}

/** A block, and whether it is still arriving (a thinking block before its end, a step before its end). */
export type ViewBlock =
  | { type: 'thinking'; text: string; seconds: number | null; live: boolean }
  | { type: 'text'; text: string }
  | { type: 'step'; step: ViewStep; live: boolean };

export interface ViewMessage {
  /** Stable for the screen: a local key until the server's id is known, never changed after. */
  key: string;
  /** The server's id, once known. */
  id: string | null;
  role: 'user' | 'assistant';
  created_at: string;
  text: string;
  model: string | null;
  effort: ChatEffort | null;
  thinking: boolean | null;
  blocks: ViewBlock[];
  files: ChatFile[];
  /** The latest task list (from `tasks` events, or the last task step of a saved message). */
  tasks: ChatTask[] | null;
  cost_usd: string | null;
  stopped: ChatStopped | null;
  error: string | null;
  /** `running` while its stream is open; `failed` when the stream broke before `message_end`. */
  status: 'running' | 'done' | 'failed';
  /** Plain words for a `failed` message. */
  failure?: string;
}

/** The task list a message ends with: the last task step's list, or null when it kept none. */
export function tasksOf(blocks: readonly (ChatBlock | ViewBlock)[]): ChatTask[] | null {
  for (let at = blocks.length - 1; at >= 0; at -= 1) {
    const block = blocks[at];
    if (block.type === 'step' && block.step.tasks?.length) return block.step.tasks;
  }
  return null;
}

/** A saved message, as the screen shows it. A saved answer with words but no blocks shows its words. */
export function fromSaved(message: ChatMessage): ViewMessage {
  const saved = message.blocks ?? [];
  const blocks: ViewBlock[] = saved.length
    ? saved.map((block): ViewBlock =>
        block.type === 'thinking' ? { type: 'thinking', text: block.text ?? '', seconds: block.seconds ?? null, live: false }
        : block.type === 'step' ? { type: 'step', step: block.step, live: false }
        : { type: 'text', text: block.text ?? '' })
    : message.role === 'assistant' && message.text ? [{ type: 'text', text: message.text }] : [];
  return {
    key: message.id,
    id: message.id,
    role: message.role,
    created_at: message.created_at,
    text: message.text ?? '',
    model: message.model ?? null,
    effort: message.effort ?? null,
    thinking: message.thinking ?? null,
    blocks,
    files: message.files ?? [],
    tasks: tasksOf(saved),
    cost_usd: message.cost_usd ?? null,
    stopped: message.stopped ?? null,
    error: message.error ?? null,
    status: 'done',
  };
}

/** The user's question, shown at once before the server has stored it. */
export function localQuestion(key: string, text: string, now = new Date()): ViewMessage {
  return {
    key, id: null, role: 'user', created_at: now.toISOString(), text, model: null, effort: null, thinking: null,
    blocks: [], files: [], tasks: null, cost_usd: null, stopped: null, error: null, status: 'done',
  };
}

/** The answer about to stream in, with the choices it was asked with. */
export function localAnswer(key: string, choice: { model: string; effort: ChatEffort; thinking: boolean }, now = new Date()): ViewMessage {
  return {
    key, id: null, role: 'assistant', created_at: now.toISOString(), text: '', ...choice,
    blocks: [], files: [], tasks: null, cost_usd: null, stopped: null, error: null, status: 'running',
  };
}

/** `blocks` with `block` at `index`. A gap (an index past the end) is filled with empty words, which show nothing. */
function withBlock(blocks: readonly ViewBlock[], index: number, block: ViewBlock): ViewBlock[] {
  const next = blocks.slice();
  while (next.length < index) next.push({ type: 'text', text: '' });
  next[index] = block;
  return next;
}

function stepAt(blocks: readonly ViewBlock[], index: number): ViewStep | null {
  const block = blocks[index];
  return block?.type === 'step' ? block.step : null;
}

function withAgents(message: ViewMessage, index: number, change: (agents: ViewAgent[]) => ViewAgent[]): ViewMessage {
  const step = stepAt(message.blocks, index);
  if (!step) return message;
  const block = message.blocks[index] as Extract<ViewBlock, { type: 'step' }>;
  return { ...message, blocks: withBlock(message.blocks, index, { ...block, step: { ...step, agents: change(step.agents ?? []) } }) };
}

/** One event applied to the answer it belongs to. Pure: the message given is never changed. */
export function applyChatEvent(message: ViewMessage, event: ChatEvent): ViewMessage {
  switch (event.event) {
    case 'message_start':
      return { ...message, id: event.data.message_id, model: event.data.model, effort: event.data.effort, thinking: event.data.thinking };
    case 'thinking_start':
      return { ...message, blocks: withBlock(message.blocks, event.data.index, { type: 'thinking', text: '', seconds: null, live: true }) };
    case 'thinking_delta': {
      const block = message.blocks[event.data.index];
      const text = (block?.type === 'thinking' ? block.text : '') + event.data.text;
      const seconds = block?.type === 'thinking' ? block.seconds : null;
      const live = block?.type === 'thinking' ? block.live : true;
      return { ...message, blocks: withBlock(message.blocks, event.data.index, { type: 'thinking', text, seconds, live }) };
    }
    case 'thinking_end': {
      const block = message.blocks[event.data.index];
      const text = block?.type === 'thinking' ? block.text : '';
      return { ...message, blocks: withBlock(message.blocks, event.data.index, { type: 'thinking', text, seconds: event.data.seconds, live: false }) };
    }
    case 'text_delta': {
      const block = message.blocks[event.data.index];
      const text = (block?.type === 'text' ? block.text : '') + event.data.text;
      return { ...message, blocks: withBlock(message.blocks, event.data.index, { type: 'text', text }) };
    }
    case 'step_start':
      return { ...message, blocks: withBlock(message.blocks, event.data.index, { type: 'step', step: event.data.step, live: true }) };
    case 'step_end': {
      // The finished step is whole; helpers seen live are kept only if it somehow came without them.
      const before = stepAt(message.blocks, event.data.index);
      const agents = event.data.step.agents?.length ? event.data.step.agents : before?.agents?.map((agent) => ({ ...agent, running: false })) ?? event.data.step.agents;
      return { ...message, blocks: withBlock(message.blocks, event.data.index, { type: 'step', step: { ...event.data.step, agents }, live: false }) };
    }
    case 'tasks':
      return { ...message, tasks: event.data.tasks };
    case 'agent_start': {
      const { agent } = event.data;
      return withAgents(message, event.data.step_index, (agents) =>
        agents.some((known) => known.id === agent.id) ? agents : [...agents, { ...agent, ok: null, report: null, steps: [], running: true }]);
    }
    case 'agent_step':
      return withAgents(message, event.data.step_index, (agents) =>
        agents.map((agent) => {
          if (agent.id !== event.data.agent_id) return agent;
          const known = agent.steps.some((step) => step.id === event.data.step.id);
          return { ...agent, steps: known ? agent.steps.map((step) => (step.id === event.data.step.id ? event.data.step : step)) : [...agent.steps, event.data.step] };
        }));
    case 'agent_end':
      return withAgents(message, event.data.step_index, (agents) => {
        const ended = { ...event.data.agent, running: false };
        return agents.some((agent) => agent.id === ended.id)
          ? agents.map((agent) => (agent.id === ended.id ? { ...agent, ...ended } : agent))
          : [...agents, { ...ended, steps: [] }];
      });
    case 'file':
      return message.files.some((file) => file.id === event.data.file.id) ? message : { ...message, files: [...message.files, event.data.file] };
    case 'usage':
      return { ...message, cost_usd: event.data.cost_usd };
    case 'error':
      return { ...message, error: event.data.message };
    case 'message_end': {
      // The stored message wins; the live task list and files stay if it somehow left them out.
      const stored = fromSaved(event.data.message);
      return {
        ...stored,
        key: message.key,
        tasks: stored.tasks ?? message.tasks,
        files: stored.files.length ? stored.files : message.files,
        error: stored.error ?? message.error,
      };
    }
    default:
      return message;
  }
}

/**
 * A message whose stream ended without `message_end` (stopped, or cut off): nothing in it is still
 * arriving any more. A failed message stays failed; anything else is done.
 */
export function settle(message: ViewMessage, change: Partial<ViewMessage> = {}): ViewMessage {
  const blocks = message.blocks.map((block): ViewBlock =>
    block.type === 'thinking' ? { ...block, live: false }
    : block.type === 'step' ? { ...block, live: false, step: { ...block.step, agents: block.step.agents?.map((agent) => ({ ...agent, running: false })) ?? block.step.agents } }
    : block);
  return { ...message, status: 'done', ...change, blocks };
}

// ── What the answer shows, in order ─────────────────────────

/**
 * One item of an answer, in the order its blocks happened: words, or one folded row holding all the
 * work between two pieces of words (thinking, tool steps, the task list's updates and helpers), as
 * claude.ai folds it ("Thought for 7s, ran 2 commands").
 */
export type AnswerItem =
  | { kind: 'activity'; indexes: number[] }
  | { kind: 'text'; index: number };

const isTaskStep = (block: ViewBlock | undefined) => block?.type === 'step' && (block.step.tool === 'TaskCreate' || block.step.tool === 'TaskUpdate');

/**
 * The answer's items: every run of thinking and steps between words folds into one activity row
 * (empty words do not break a run). `planAfter` is the item after which the task list shows: the
 * one holding the last task step, or the last item when only `tasks` events said there is a list.
 */
export function answerItems(blocks: readonly ViewBlock[], hasTasks: boolean): { items: AnswerItem[]; planAfter: number | null } {
  const items: AnswerItem[] = [];
  let lastTaskItem: number | null = null;
  blocks.forEach((block, index) => {
    if (block.type === 'text') {
      if (block.text.trim()) items.push({ kind: 'text', index });
      return;
    }
    const last = items[items.length - 1];
    if (last?.kind === 'activity') last.indexes.push(index);
    else items.push({ kind: 'activity', indexes: [index] });
    if (isTaskStep(block)) lastTaskItem = items.length - 1;
  });
  const planAfter = lastTaskItem ?? (hasTasks && items.length ? items.length - 1 : null);
  return { items, planAfter };
}

/** The engine's own italic stop note at the end of a cut-short answer ("*Stopped early: …*"). */
const STOP_NOTE = /\n*\s*([*_])Stopped early\b[^\n]*\1\s*$/;

/**
 * The blocks an answer shows. A stopped answer's last words lose the engine's italic "*Stopped
 * early…*" line, because the screen says why it stopped once, in its own words, under the answer.
 */
export function shownBlocks(message: Pick<ViewMessage, 'blocks' | 'stopped'>): ViewBlock[] {
  if (!message.stopped) return message.blocks as ViewBlock[];
  const blocks = message.blocks.slice();
  for (let at = blocks.length - 1; at >= 0; at -= 1) {
    const block = blocks[at];
    if (block.type !== 'text') continue;
    if (!block.text.trim()) continue;
    blocks[at] = { type: 'text', text: withoutStopNote(block.text) };
    break;
  }
  return blocks;
}

/** Words without a trailing "*Stopped early: …*" line. */
export function withoutStopNote(text: string): string {
  return text.replace(STOP_NOTE, '').trimEnd();
}

/** True while the newest words are still arriving: the answer runs and its last block is words. */
export function textStreaming(message: Pick<ViewMessage, 'status' | 'blocks'>): boolean {
  const last = message.blocks[message.blocks.length - 1];
  return message.status === 'running' && last?.type === 'text';
}

// ── The activity row's words ────────────────────────────────

type Phrase = { one: string; many: (count: number) => string } | { once: string };

function phraseFor(step: ChatStep): { key: string; phrase: Phrase } {
  switch (step.tool) {
    case 'bash_tool':
      return { key: 'bash', phrase: { one: 'ran a command', many: (n) => `ran ${n} commands` } };
    case 'view':
      return step.picture_id
        ? { key: 'picture', phrase: { one: 'viewed a picture', many: (n) => `viewed ${n} pictures` } }
        : { key: 'read', phrase: { one: 'viewed a file', many: (n) => `viewed ${n} files` } };
    case 'create_file':
      return { key: 'create', phrase: { one: 'created a file', many: (n) => `created ${n} files` } };
    case 'str_replace':
      return { key: 'edit', phrase: { one: 'edited a file', many: (n) => `edited ${n} files` } };
    case 'present_files':
      return { key: 'present', phrase: { once: 'presented files' } };
    case 'TaskCreate':
    case 'TaskUpdate':
      return { key: 'tasks', phrase: { once: 'updated the task list' } };
    case 'Agent':
      return { key: 'agents', phrase: { one: 'ran an agent', many: (n) => `ran ${n} agents` } };
    default:
      return { key: 'results', phrase: { once: 'read the review results' } };
  }
}

/** "Thought for 7s" for every thinking block of a run together; "Thought" when no time is known. */
function thoughtTotal(blocks: readonly ViewBlock[]): string | null {
  const thinking = blocks.filter((block): block is Extract<ViewBlock, { type: 'thinking' }> => block.type === 'thinking');
  if (!thinking.length) return null;
  const known = thinking.map((block) => block.seconds).filter((seconds): seconds is number => seconds !== null && Number.isFinite(seconds));
  return thoughtFor(known.length ? known.reduce((sum, seconds) => sum + seconds, 0) : null);
}

const SUMMARY_PARTS = 4;

/**
 * A finished activity row in words, claude.ai's way: "Thought for 7s, ran 3 commands, viewed 2
 * pictures, created a file". Thinking first, then each kind of tool in the order it was first used
 * (counted ones before the task list, presented files and review results), four parts at most.
 */
export function activitySummary(blocks: readonly ViewBlock[]): string {
  const counts = new Map<string, { phrase: Phrase; count: number }>();
  for (const block of blocks) {
    if (block.type !== 'step') continue;
    const { key, phrase } = phraseFor(block.step);
    const count = block.step.tool === 'Agent' ? Math.max(1, block.step.agents?.length ?? 0) : 1;
    const known = counts.get(key);
    if (known) known.count += count;
    else counts.set(key, { phrase, count });
  }
  // Counted work first (commands, pictures, files, helpers), then the rest; four parts at most, so
  // the row stays one short line.
  const counted = [...counts.values()].filter(({ phrase }) => !('once' in phrase));
  const once = [...counts.values()].filter(({ phrase }) => 'once' in phrase);
  const parts = [thoughtTotal(blocks), ...[...counted, ...once].map(({ phrase, count }) =>
    'once' in phrase ? phrase.once : count === 1 ? phrase.one : phrase.many(count))].filter(Boolean).slice(0, SUMMARY_PARTS);
  const words = parts.join(', ');
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : 'Worked';
}

/** The latest line of a thinking summary, without its markdown marks; null when there is none yet. */
export function thinkingLine(text: string): string | null {
  const lines = text.split('\n').map((line) => line.replace(/^\s{0,3}(?:#{1,6}\s+|[-*+]\s+|>\s?)/, '').replace(/\*\*|__|`/g, '').trim()).filter(Boolean);
  return lines.length ? lines[lines.length - 1] : null;
}

/** "Running 4 agents…" while any helper works, "Ran 4 agents" after. */
export function agentsWords(step: Pick<ViewStep, 'agents'>, running: boolean): string {
  const count = step.agents?.length ?? 0;
  const noun = count === 1 ? 'agent' : 'agents';
  return running ? `Running ${count || ''}${count ? ' ' : ''}${noun}…` : `Ran ${count} ${noun}`;
}

/** True while an Agent step's helpers are still working. */
export function agentsRunning(step: ViewStep, live: boolean): boolean {
  return live || Boolean(step.agents?.some((agent) => agent.running));
}

/**
 * What a running activity row says: the newest thing it does, as it does it (the newest thinking
 * line, or the running step's own description, or "Running 4 agents…").
 */
export function activityHeadline(blocks: readonly ViewBlock[]): string {
  const last = blocks[blocks.length - 1];
  if (!last) return 'Working…';
  if (last.type === 'thinking') return (last.live ? thinkingLine(last.text) : null) ?? (last.live ? 'Thinking…' : thoughtFor(last.seconds));
  if (last.type === 'step') return last.step.tool === 'Agent' ? agentsWords(last.step, agentsRunning(last.step, last.live)) : last.step.label;
  return 'Working…';
}

/** The question an answer replied to (the user message before it), for Retry; null when there is none. */
export function previousQuestion(messages: readonly Pick<ViewMessage, 'key' | 'role' | 'text'>[], answerKey: string): string | null {
  const at = messages.findIndex((message) => message.key === answerKey);
  for (let index = at - 1; index >= 0; index -= 1) {
    if (messages[index].role === 'user') return messages[index].text;
  }
  return null;
}

// ── Words ───────────────────────────────────────────────────

/** The three questions an empty chat offers (the contract's words). */
export const CHAT_STARTERS: readonly string[] = [
  'Which items are wrong comparing the vendor and architect drawings?',
  'Summarise every countertop and cabinet issue',
  'Make me a marked-up PDF of the problems',
];

/** A chat's name: its title (its first question), else "New chat". */
export function chatTitle(chat: Pick<ChatSummary, 'title'>): string {
  return chat.title?.trim() || 'New chat';
}

/** A model id in words: the server's label, else made from the id ("claude-opus-5-5" → "Claude Opus 5.5"). */
export function modelLabel(id: string | null, models: readonly Pick<ChatModel, 'id' | 'label'>[] | null): string {
  if (!id) return 'Claude';
  const known = models?.find((model) => model.id === id);
  if (known) return known.label;
  const words: string[] = [];
  let version: string[] = [];
  for (const part of id.replace(/^anthropic[./]/, '').split(/[-_.]/)) {
    if (/^\d+$/.test(part)) version.push(part);
    else {
      if (version.length) words.push(version.join('.'));
      version = [];
      words.push(part.charAt(0).toUpperCase() + part.slice(1));
    }
  }
  if (version.length) words.push(version.join('.'));
  return words.join(' ');
}

/** Dollars as "$1.72" (a cost under a cent is "under $0.01"); null when there is no number. */
export function dollars(value: string | null | undefined): string | null {
  if (value === null || value === undefined || value === '') return null;
  const amount = Number(value);
  if (!Number.isFinite(amount)) return null;
  if (amount > 0 && amount < 0.005) return 'under $0.01';
  return `$${amount.toFixed(2)}`;
}

/** The label the page shows under the composer (claude.ai's disclaimer); no longer on each answer. */
export const AI_ANSWER_WORDS = 'AI answer: check before acting';

/** A message's cost for its meta line: "$1.72", or "<$0.01" below one cent; null when not known. */
export function messageCost(value: string | null | undefined): string | null {
  if (value === null || value === undefined || value === '') return null;
  const amount = Number(value);
  if (!Number.isFinite(amount) || amount < 0) return null;
  // The server rounds to cents, so a message that cost $0.002 arrives as "0.00".
  if (amount < 0.01) return '<$0.01';
  return `$${amount.toFixed(2)}`;
}

/** The small line under a finished answer: "Claude Opus 5.5 · High effort · $1.72". */
export function footerWords(message: Pick<ViewMessage, 'model' | 'effort' | 'cost_usd'>, models: readonly Pick<ChatModel, 'id' | 'label'>[] | null): string {
  return [
    modelLabel(message.model, models),
    message.effort ? `${EFFORT_WORDS[message.effort]} effort` : null,
    messageCost(message.cost_usd),
  ].filter(Boolean).join(' · ');
}

/** A cap in words: "$5", or "$7.50" when it has cents. */
function capWords(value: string | null | undefined): string | null {
  const amount = Number(value);
  if (value === null || value === undefined || value === '' || !Number.isFinite(amount)) return null;
  return Number.isInteger(amount) ? `$${amount}` : `$${amount.toFixed(2)}`;
}

/** Why an answer stopped, in claude.ai's plain words; null when it finished. */
export function stoppedWords(message: Pick<ViewMessage, 'stopped'>, messageCapUsd?: string | null): string | null {
  switch (message.stopped) {
    case 'cost_cap': {
      const cap = capWords(messageCapUsd);
      return cap ? `Stopped: this answer reached its ${cap} limit` : 'Stopped: this answer reached its cost limit';
    }
    case 'cancelled':
      return 'Stopped';
    case 'turn_limit':
      return 'Stopped after the maximum number of steps';
    case 'error':
      return 'Something went wrong. Try again.';
    default:
      return null;
  }
}

/** "Thought for 12s", "Thought for 1m 5s"; "Thought" when the time is not known. */
export function thoughtFor(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds)) return 'Thought';
  const whole = Math.max(1, Math.round(seconds));
  if (whole < 60) return `Thought for ${whole}s`;
  return `Thought for ${Math.floor(whole / 60)}m ${whole % 60}s`;
}

/** A file's size in words: "820 KB", "1.2 MB". */
export function fileSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return '';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** A file's kind in a word, from its type or its name: "PDF", "CSV", "Image". */
export function fileKind(file: Pick<ChatFile, 'mime' | 'name'>): string {
  const mime = file.mime.toLowerCase();
  if (mime === 'application/pdf') return 'PDF';
  if (mime.startsWith('image/')) return 'Image';
  if (mime === 'text/csv') return 'CSV';
  if (mime.includes('spreadsheet') || mime.includes('excel')) return 'Spreadsheet';
  if (mime === 'text/markdown') return 'Markdown';
  if (mime.startsWith('text/')) return 'Text';
  const extension = /\.([a-z0-9]{1,5})$/i.exec(file.name)?.[1];
  return extension ? extension.toUpperCase() : 'File';
}

/**
 * When a chat was last used, said the short way: "just now", "5 min ago", "3 h ago", "Yesterday",
 * then the day ("Mon") within a week, then the date.
 */
export function relativeTime(iso: string, now: Date = new Date()): string {
  const at = new Date(iso);
  const ms = now.getTime() - at.getTime();
  if (!Number.isFinite(ms)) return '';
  const minutes = Math.floor(ms / 60_000);
  if (minutes < 1) return 'just now';
  if (minutes < 60) return `${minutes} min ago`;
  const startOfToday = new Date(now);
  startOfToday.setHours(0, 0, 0, 0);
  if (at >= startOfToday) return `${Math.floor(minutes / 60)} h ago`;
  const startOfYesterday = new Date(startOfToday);
  startOfYesterday.setDate(startOfYesterday.getDate() - 1);
  if (at >= startOfYesterday) return 'Yesterday';
  const weekAgo = new Date(startOfToday);
  weekAgo.setDate(weekAgo.getDate() - 6);
  if (at >= weekAgo) return at.toLocaleDateString(undefined, { weekday: 'short' });
  return at.toLocaleDateString(undefined, at.getFullYear() === now.getFullYear() ? { day: 'numeric', month: 'short' } : { day: 'numeric', month: 'short', year: 'numeric' });
}
