/**
 * A chat message as the screen shows it (#1464), built from the stream's events or from the saved
 * message, kept free of React so it can be tested alone.
 *
 * The stream sends each block by its place in `blocks` (`index`), so thinking, words and tool steps
 * stay in the order they happened, exactly as a reload shows them. `message_end` carries the stored
 * message, which then replaces what was built live: a reload and a live answer end up identical.
 */

import type { ChatAccuracy, ChatAgent, ChatBlock, ChatCite, ChatCoverage, ChatEffort, ChatEvent, ChatFile, ChatFinding, ChatFindingUpdate, ChatMessage, ChatMode, ChatModel, ChatProduct, ChatProgress, ChatResume, ChatScope, ChatStep, ChatStopped, ChatSummary, ChatTask, ChatTrust, ChatUnderstood, ChatUnknown } from '@/lib/chat-api';
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
  | { type: 'text'; text: string; by?: 'code' | null }
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
  /**
   * Chat v2: the answer's findings at their latest version, in the order they were first reported
   * (an update replaces its finding in place; a dropped one is gone). Absent means none.
   */
  findings?: ChatFinding[];
  /** Chat v2: the page-by-page coverage map of a whole-set answer; absent or null when it kept none. */
  coverage?: ChatCoverage | null;
  /** Chat v3: the trust report code built after the answer's checks; absent or null before. */
  trust?: ChatTrust | null;
  /** Chat v4: how it was asked (a full check, or absent for an ordinary message) and for which products. */
  mode?: ChatMode | null;
  products?: ChatProduct[] | null;
  /** #1522: what the answer covers, when the server sends it (else worked out from its pages). */
  scope?: ChatScope | null;
  /** #1524: a full check's latest progress (live while it runs; the stored one after). */
  progress?: ChatProgress | null;
  /** Chat v5: the numbers the answer backed with a spot on a drawing (latest version by id). */
  cites?: ChatCite[];
  /** Chat v5: code's accuracy score; absent on older answers (they show no badge). */
  accuracy?: ChatAccuracy | null;
  /** What the answer did not check, as records (code's `unknowns` block); absent on older answers. */
  unknowns?: ChatUnknown[];
  /** With `stopped: "interrupted"`: how Continue picks up (#1514). */
  resume?: ChatResume | null;
  /** #1548: the earlier answers' findings this answer corrected (newest per ref). */
  corrections?: ChatFindingUpdate[];
  /** #1551: how the app understood the question before the work; shown under the question. */
  understood?: ChatUnderstood | null;
  cost_usd: string | null;
  stopped: ChatStopped | null;
  error: string | null;
  /** The caps it ran under, and which one stopped it (saved answers; absent while it streams). */
  message_cap_usd?: string | null;
  package_cap_usd?: string | null;
  cap_hit?: 'message' | 'package' | null;
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

/** A finding added, or put in place of its earlier version (same id); the list given is never changed. */
export function withFinding(findings: readonly ChatFinding[], finding: ChatFinding): ChatFinding[] {
  return findings.some((known) => known.id === finding.id)
    ? findings.map((known) => (known.id === finding.id ? finding : known))
    : [...findings, finding];
}

/** A correction added, or put in place of its earlier version (same ref). */
export function withCorrection(corrections: readonly ChatFindingUpdate[], update: ChatFindingUpdate): ChatFindingUpdate[] {
  return corrections.some((known) => known.ref === update.ref)
    ? corrections.map((known) => (known.ref === update.ref ? update : known))
    : [...corrections, update];
}

/**
 * #1548: a later message corrected a finding of an earlier answer. That answer shows the newest
 * version ("updated in a later message"), or loses it when it was dropped. The server reads it the
 * same way on reload; this keeps the screen in step while the later answer streams.
 */
export function applyCorrection(messages: readonly ViewMessage[], update: ChatFindingUpdate, laterId: string | null = null): ViewMessage[] {
  return messages.map((message) => {
    if (message.id !== update.message_id || !message.findings) return message;
    if (update.dropped) return { ...message, findings: message.findings.filter((finding) => finding.id !== update.finding_id) };
    const newest: ChatFinding = {
      ...update.finding,
      id: update.finding_id,
      updated_in: { message_id: laterId ?? '', answer: update.by_answer ?? null, version: update.version },
    };
    return { ...message, findings: message.findings.map((finding) => (finding.id === update.finding_id ? newest : finding)) };
  });
}

/** The corrections a saved answer made (#1548), newest per ref. */
export function correctionsOf(blocks: readonly ChatBlock[]): ChatFindingUpdate[] {
  let corrections: ChatFindingUpdate[] = [];
  for (const block of blocks) if (block.type === 'finding_update' && block.update?.ref) corrections = withCorrection(corrections, block.update);
  return corrections;
}

/** A cite added, or put in place of its earlier version (same id). */
export function withCite(cites: readonly ChatCite[], cite: ChatCite): ChatCite[] {
  return cites.some((known) => known.id === cite.id) ? cites.map((known) => (known.id === cite.id ? cite : known)) : [...cites, cite];
}

/**
 * The findings and coverage a saved message's blocks hold: each finding at its latest version (a
 * later block with the same id replaces it), and the last coverage block. A dropped finding is
 * simply not in the stored blocks; a `finding_dropped` block, if a server ever stores one, removes it.
 */
export function evidenceOf(blocks: readonly ChatBlock[]): { findings: ChatFinding[]; coverage: ChatCoverage | null; trust: ChatTrust | null; cites: ChatCite[]; accuracy: ChatAccuracy | null; unknowns: ChatUnknown[]; progress: ChatProgress | null } {
  let findings: ChatFinding[] = [];
  let coverage: ChatCoverage | null = null;
  let trust: ChatTrust | null = null;
  let cites: ChatCite[] = [];
  let accuracy: ChatAccuracy | null = null;
  let unknowns: ChatUnknown[] = [];
  let progress: ChatProgress | null = null;
  for (const block of blocks as readonly (ChatBlock | { type: 'finding_dropped'; finding_id: string })[]) {
    if (block.type === 'finding' && block.finding?.id) findings = withFinding(findings, block.finding);
    else if (block.type === 'finding_dropped') findings = findings.filter((finding) => finding.id !== block.finding_id);
    else if (block.type === 'coverage' && block.coverage) coverage = block.coverage;
    else if (block.type === 'trust' && block.trust) trust = block.trust;
    else if (block.type === 'cite' && block.cite?.id) cites = withCite(cites, block.cite);
    else if (block.type === 'accuracy' && block.accuracy) accuracy = block.accuracy;
    else if (block.type === 'unknowns' && Array.isArray(block.unknowns)) unknowns = block.unknowns;
    else if (block.type === 'progress' && block.progress) progress = block.progress;
  }
  return { findings, coverage, trust, cites, accuracy, unknowns, progress };
}

/** The last scope block of a saved message (#1528), when the message itself carries none. */
function scopeOf(blocks: readonly ChatBlock[]): ChatScope | null {
  let scope: ChatScope | null = null;
  for (const block of blocks) if (block.type === 'scope' && block.scope) scope = block.scope;
  return scope;
}

/** The `understood` block of an answer (#1551), or null. */
export function understoodOf(blocks: readonly ChatBlock[]): ChatUnderstood | null {
  for (const block of blocks) if (block.type === 'understood' && block.understood) return block.understood;
  return null;
}

/** The `resume` block of an interrupted answer (#1514), or null. */
export function resumeOf(blocks: readonly ChatBlock[]): ChatResume | null {
  for (let at = blocks.length - 1; at >= 0; at -= 1) {
    const block = blocks[at];
    if (block.type === 'resume' && block.resume) return block.resume;
  }
  return null;
}

/** A saved message, as the screen shows it. A saved answer with words but no blocks shows its words. */
export function fromSaved(message: ChatMessage): ViewMessage {
  const saved = message.blocks ?? [];
  // Findings and coverage are drawn by the screen in their own places, not in the order of the work.
  const shown = saved.filter((block) => block.type === 'thinking' || block.type === 'text' || block.type === 'step');
  const { findings, coverage, trust, cites, accuracy, unknowns, progress } = evidenceOf(saved);
  const blocks: ViewBlock[] = shown.length
    ? shown.map((block): ViewBlock =>
        block.type === 'thinking' ? { type: 'thinking', text: block.text ?? '', seconds: block.seconds ?? null, live: false }
        : block.type === 'step' ? { type: 'step', step: block.step, live: false }
        : { type: 'text', text: block.type === 'text' ? block.text ?? '' : '', ...(block.type === 'text' && block.by ? { by: block.by } : {}) })
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
    findings,
    coverage,
    trust,
    mode: message.mode ?? null,
    scope: message.scope ?? scopeOf(saved),
    products: message.products ?? null,
    cites,
    accuracy: message.accuracy ?? accuracy,
    unknowns,
    progress,
    resume: message.resume ?? resumeOf(saved),
    corrections: correctionsOf(saved),
    understood: message.understood ?? understoodOf(saved),
    cost_usd: message.cost_usd ?? null,
    stopped: message.stopped ?? null,
    error: message.error ?? null,
    message_cap_usd: message.message_cap_usd ?? null,
    package_cap_usd: message.package_cap_usd ?? null,
    cap_hit: message.cap_hit ?? null,
    status: 'done',
  };
}

/** How a message was asked (chat v4): a full check and its products, and the effort it ran at. */
export interface AskedAs {
  mode?: ChatMode | null;
  products?: ChatProduct[] | null;
  effort?: ChatEffort | null;
}

/** The user's question, shown at once before the server has stored it. */
export function localQuestion(key: string, text: string, now = new Date(), asked: AskedAs = {}): ViewMessage {
  return {
    key, id: null, role: 'user', created_at: now.toISOString(), text, model: null, effort: asked.effort ?? null, thinking: null,
    blocks: [], files: [], tasks: null, cost_usd: null, stopped: null, error: null, status: 'done',
    mode: asked.mode ?? null, products: asked.products ?? null,
  };
}

/** The answer about to stream in, with the choices it was asked with. */
export function localAnswer(key: string, choice: { model: string; effort: ChatEffort; thinking: boolean }, now = new Date(), asked: AskedAs = {}): ViewMessage {
  return {
    key, id: null, role: 'assistant', created_at: now.toISOString(), text: '', model: choice.model, effort: choice.effort, thinking: choice.thinking,
    blocks: [], files: [], tasks: null, cost_usd: null, stopped: null, error: null, status: 'running',
    mode: asked.mode ?? null, products: asked.products ?? null,
  };
}

/**
 * An answer still being written when the chat was opened (a reload, another tab): its events are
 * replayed from the server and then follow live, exactly like an answer sent from this screen.
 */
export function followedAnswer(messageId: string, now = new Date()): ViewMessage {
  return {
    key: `follow-${messageId}`, id: messageId, role: 'assistant', created_at: now.toISOString(), text: '', model: null, effort: null, thinking: null,
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
    case 'finding':
      return event.data.finding?.id ? { ...message, findings: withFinding(message.findings ?? [], event.data.finding) } : message;
    case 'finding_dropped':
      return { ...message, findings: (message.findings ?? []).filter((finding) => finding.id !== event.data.finding_id) };
    case 'finding_update':
      return event.data.update?.ref ? { ...message, corrections: withCorrection(message.corrections ?? [], event.data.update) } : message;
    case 'coverage':
      return { ...message, coverage: event.data.coverage ?? message.coverage ?? null };
    case 'scope':
      return event.data.scope ? { ...message, scope: event.data.scope } : message;
    case 'understood':
      return event.data.understood ? { ...message, understood: event.data.understood } : message;
    case 'progress':
      return { ...message, progress: event.data.progress ?? message.progress ?? null };
    case 'trust':
      return { ...message, trust: event.data.trust ?? message.trust ?? null };
    case 'cite':
      return event.data.cite?.id ? { ...message, cites: withCite(message.cites ?? [], event.data.cite) } : message;
    case 'accuracy':
      return { ...message, accuracy: event.data.accuracy ?? message.accuracy ?? null };
    case 'unknowns':
      return Array.isArray(event.data.unknowns) ? { ...message, unknowns: event.data.unknowns } : message;
    case 'message_end': {
      // The stored message wins; the live task list, files, findings and coverage stay if it somehow
      // left them out.
      const stored = fromSaved(event.data.message);
      return {
        ...stored,
        key: message.key,
        tasks: stored.tasks ?? message.tasks,
        files: stored.files.length ? stored.files : message.files,
        findings: stored.findings?.length ? stored.findings : message.findings ?? [],
        coverage: stored.coverage ?? message.coverage ?? null,
        trust: stored.trust ?? message.trust ?? null,
        mode: stored.mode ?? message.mode ?? null,
        scope: stored.scope ?? message.scope ?? null,
        understood: stored.understood ?? message.understood ?? null,
        cites: stored.cites?.length ? stored.cites : message.cites ?? [],
        corrections: stored.corrections?.length ? stored.corrections : message.corrections ?? [],
        accuracy: stored.accuracy ?? message.accuracy ?? null,
        unknowns: stored.unknowns?.length ? stored.unknowns : message.unknowns ?? [],
        progress: stored.progress ?? message.progress ?? null,
        products: stored.products ?? message.products ?? null,
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
    blocks[at] = { ...block, type: 'text', text: withoutStopNote(block.text) };
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
    case 'read_under_markup':
      return { key: 'under', phrase: { one: "read under GV's markup", many: (n) => `read under GV's markup ${n} times` } };
    case 'remember':
      return { key: 'remember', phrase: { one: 'kept a note for this set', many: (n) => `kept ${n} notes for this set` } };
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

/**
 * Why an answer stopped, in claude.ai's plain words; null when it finished. A money stop names the cap
 * the answer actually ran under (kept with it). An older answer saved without its cap says "its cost
 * limit": today's setting may differ from the cap it ran under (#1483: a $5 stop read "$15").
 */
export function stoppedWords(
  message: Pick<ViewMessage, 'stopped'> & Partial<Pick<ViewMessage, 'cap_hit' | 'message_cap_usd' | 'package_cap_usd'>>,
): string | null {
  switch (message.stopped) {
    case 'cost_cap': {
      if (message.cap_hit === 'package') {
        const budget = capWords(message.package_cap_usd);
        return budget ? `Stopped: this drawing set has used its ${budget} AI budget` : 'Stopped: this drawing set has used its AI budget';
      }
      const cap = capWords(message.message_cap_usd);
      return cap ? `Stopped: this answer reached its ${cap} limit` : 'Stopped: this answer reached its cost limit';
    }
    case 'cancelled':
      return 'Stopped';
    case 'turn_limit':
      return 'Stopped after the maximum number of steps';
    case 'interrupted':
      return 'Interrupted by a server restart';
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
