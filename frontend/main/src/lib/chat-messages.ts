/**
 * A chat message as the screen shows it (#1464), built from the stream's events or from the saved
 * message, kept free of React so it can be tested alone.
 *
 * The stream sends each block by its place in `blocks` (`index`), so thinking, words and tool steps
 * stay in the order they happened, exactly as a reload shows them. `message_end` carries the stored
 * message, which then replaces what was built live: a reload and a live answer end up identical.
 */

import type { AssistantPlanTask, AssistantStep, AssistantSubAgent } from '@/api/assistantTypes';
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
 * One item of an answer, in the order its blocks happened: a thinking block, words, one step, or
 * (once the answer is finished) a run of finished steps folded under one line.
 */
export type AnswerItem =
  | { kind: 'thinking'; index: number }
  | { kind: 'text'; index: number }
  | { kind: 'step'; index: number }
  | { kind: 'steps'; indexes: number[] };

const isTaskStep = (block: ViewBlock | undefined) => block?.type === 'step' && (block.step.tool === 'TaskCreate' || block.step.tool === 'TaskUpdate');

/**
 * The answer's items. While it runs, every step is its own line (so the newest is in view); once
 * finished, each run of steps between words is folded under one line, as claude.ai does. Empty
 * words are left out. `planAfter` is the item after which the task list shows: the one holding the
 * last task step, or the last item when only `tasks` events said there is a list.
 */
export function answerItems(blocks: readonly ViewBlock[], running: boolean, hasTasks: boolean): { items: AnswerItem[]; planAfter: number | null } {
  const items: AnswerItem[] = [];
  let lastTaskItem: number | null = null;
  blocks.forEach((block, index) => {
    if (block.type === 'text') {
      if (block.text.trim()) items.push({ kind: 'text', index });
      return;
    }
    if (block.type === 'thinking') {
      items.push({ kind: 'thinking', index });
      return;
    }
    const last = items[items.length - 1];
    if (!running && !block.live && last?.kind === 'steps') last.indexes.push(index);
    else items.push(running || block.live ? { kind: 'step', index } : { kind: 'steps', indexes: [index] });
    if (isTaskStep(block)) lastTaskItem = items.length - 1;
  });
  const planAfter = lastTaskItem ?? (hasTasks && items.length ? items.length - 1 : null);
  return { items, planAfter };
}

/** True while the newest words are still arriving: the answer runs and its last block is words. */
export function textStreaming(message: Pick<ViewMessage, 'status' | 'blocks'>): boolean {
  const last = message.blocks[message.blocks.length - 1];
  return message.status === 'running' && last?.type === 'text';
}

/** True while something shows its own progress (a thinking block or a step still arriving, or words). */
export function somethingLive(message: Pick<ViewMessage, 'status' | 'blocks'>): boolean {
  if (message.status !== 'running') return false;
  if (textStreaming(message)) return true;
  return message.blocks.some((block) => block.type !== 'text' && block.live);
}

// ── Steps, in the assistant panel's shapes ──────────────────

const STEP_KIND: Record<ChatStep['tool'], AssistantStep['kind']> = {
  bash_tool: 'bash',
  view: 'view',
  review_results: 'review_results',
  TaskCreate: 'task',
  TaskUpdate: 'task',
  Agent: 'agents',
  create_file: 'file',
  str_replace: 'file',
  present_files: 'file',
};

function planTask(task: ChatTask): AssistantPlanTask {
  return { id: task.id, subject: task.subject, active_form: task.active_form ?? null, status: task.status };
}

/** A chat step as the assistant panel's step list draws it (`assistant-steps.tsx`), its picture by URL. */
export function toAssistantStep(step: ChatStep, pictureUrl: (pictureId: string) => string): AssistantStep {
  return {
    kind: STEP_KIND[step.tool] ?? 'review_results',
    label: step.label,
    // A file step shows the file it worked on where a command step shows its command.
    command: step.command ?? step.path ?? null,
    path: step.path ?? null,
    output: step.output ?? null,
    // Null while it runs, which is not a failure.
    ok: step.ok !== false,
    thumbnail: step.picture_id ? pictureUrl(step.picture_id) : null,
    ...(step.tasks ? { tasks: step.tasks.map(planTask) } : {}),
    ...(step.agents ? { agents: step.agents.map((agent) => toSubAgent(agent, pictureUrl)) } : {}),
  };
}

function toSubAgent(agent: ChatAgent, pictureUrl: (pictureId: string) => string): AssistantSubAgent {
  return {
    description: agent.description,
    model: /opus/i.test(agent.model) ? 'opus' : 'sonnet',
    steps: agent.steps.map((step) => toAssistantStep(step, pictureUrl)),
    report: agent.report ?? '',
    ok: agent.ok === true,
    stopped: agent.stopped ? (agent.stopped === 'cost_cap' ? 'cost_cap' : 'failed') : null,
  };
}

export function toPlanTasks(tasks: readonly ChatTask[]): AssistantPlanTask[] {
  return tasks.map(planTask);
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

export const AI_ANSWER_WORDS = 'AI answer: check before acting';

/** "Claude Opus 5.5 · High effort · $1.72 · AI answer: check before acting". */
export function footerWords(message: Pick<ViewMessage, 'model' | 'effort' | 'cost_usd'>, models: readonly Pick<ChatModel, 'id' | 'label'>[] | null): string {
  return [
    modelLabel(message.model, models),
    message.effort ? `${EFFORT_WORDS[message.effort]} effort` : null,
    dollars(message.cost_usd),
    AI_ANSWER_WORDS,
  ].filter(Boolean).join(' · ');
}

/** Why an answer stopped, in plain words; null when it finished. */
export function stoppedWords(message: Pick<ViewMessage, 'stopped' | 'error'>, messageCapUsd?: string | null): string | null {
  const cap = dollars(messageCapUsd);
  switch (message.stopped) {
    case 'cost_cap':
      return `Stopped: this message reached its cost cap${cap ? ` of ${cap}` : ''}. Above is what it found before then.`;
    case 'cancelled':
      return 'Stopped by you.';
    case 'turn_limit':
      return 'Stopped: it reached the most steps one message may take. Above is what it found before then.';
    case 'error': {
      const words = message.error?.trim().replace(/\.$/, '');
      return words ? `Stopped by an error: ${words}.` : 'Stopped by an error.';
    }
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
