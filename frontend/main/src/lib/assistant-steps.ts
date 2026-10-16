import type { AssistantStep, AssistantStepEvent } from '@/api/assistantTypes';

function plural(count: number, one: string, many: string): string {
  return `${count} ${count === 1 ? one : many}`;
}

/** "Ran 4 commands, viewed 1 picture, read the results, ran 3 agents". */
export function stepsSummary(steps: AssistantStep[]): string {
  const count = (kind: AssistantStep['kind']) => steps.filter((step) => step.kind === kind).length;
  const parts: string[] = [];
  const commands = count('bash');
  const pictures = count('view');
  const agents = steps.filter((step) => step.kind === 'agents').reduce((total, step) => total + (step.agents?.length ?? 0), 0);
  if (commands) parts.push(`ran ${plural(commands, 'command', 'commands')}`);
  if (pictures) parts.push(`viewed ${plural(pictures, 'picture', 'pictures')}`);
  if (count('review_results')) parts.push('read the results');
  if (agents) parts.push(`ran ${plural(agents, 'agent', 'agents')}`);
  if (!parts.length && count('task')) parts.push('kept a task list');
  const text = parts.join(', ');
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : '';
}


// ── Live: the steps so far while the answer is worked out ──

export type LiveEntry =
  | { kind: 'step'; step: AssistantStep }
  | { kind: 'agents'; label: string; agents: { description: string; steps: AssistantStep[] }[] };

const RUNNING_AGENTS = /^Running \d+ agents · /;

/** A stage label: "Running N agents · …" opens the helpers' block (once). */
export function withStage(entries: readonly LiveEntry[], label: string): LiveEntry[] {
  if (!RUNNING_AGENTS.test(label)) return [...entries];
  const last = entries[entries.length - 1];
  if (last?.kind === 'agents' && last.label === label) return [...entries];
  return [...entries, { kind: 'agents', label, agents: [] }];
}

/** A finished step: a helper's goes into the running helpers' block; the lead's "Ran N agents" replaces that block. */
export function withStep(entries: readonly LiveEntry[], event: AssistantStepEvent): LiveEntry[] {
  const out = [...entries];
  const openAt = out.map((entry) => entry.kind).lastIndexOf('agents');
  if (event.agent !== null) {
    let block = openAt >= 0 ? (out[openAt] as Extract<LiveEntry, { kind: 'agents' }>) : null;
    if (!block) {
      block = { kind: 'agents', label: 'Running agents', agents: [] };
      out.push(block);
    }
    const agents = block.agents.some((agent) => agent.description === event.agent)
      ? block.agents.map((agent) => (agent.description === event.agent ? { ...agent, steps: [...agent.steps, event.step] } : agent))
      : [...block.agents, { description: event.agent, steps: [event.step] }];
    out[openAt >= 0 ? openAt : out.length - 1] = { ...block, agents };
    return out;
  }
  if (event.step.kind === 'agents' && openAt >= 0) {
    out[openAt] = { kind: 'step', step: event.step };
    return out;
  }
  return [...out, { kind: 'step', step: event.step }];
}
