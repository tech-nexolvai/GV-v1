import type { AssistantStep } from '@/api/assistantTypes';

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

