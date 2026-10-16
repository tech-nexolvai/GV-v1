import { ChevronRight, FileSearch, Image as ImageIcon, ListChecks, SquareTerminal, Users } from 'lucide-react';

import type { AssistantPlanTask, AssistantStep } from '@/api/assistantTypes';
import { stepsSummary, type LiveEntry } from '@/lib/assistant-steps';

/**
 * The tools the assistant used for one answer, shown like claude.ai (owner, 2026-10-16). While it
 * works, a growing list: each step one line (its own description), folded, and opening to the
 * command and its output or the picture it looked at; tasks as "Added task / Started task /
 * Completed task"; "Running N agents · …" with each helper agent foldable. After the answer, the
 * same steps folded under one line ("Ran 6 commands, viewed 2 pictures"). Its task list shows as
 * "Progress". Read-only: nothing here changes the review.
 */

const STATUS_WORDS: Record<AssistantPlanTask['status'], string> = { pending: 'To do', in_progress: 'Working on it', completed: 'Done' };

export function PlanList({ tasks }: { tasks: AssistantPlanTask[] }) {
  if (!tasks.length) return null;
  const done = tasks.filter((task) => task.status === 'completed').length;
  return (
    <div data-slot="assistant-plan" className="rounded-md border border-border px-3 py-2 text-xs">
      <p className="mb-1 font-medium text-foreground">
        Progress <span className="num text-muted-foreground">{done}/{tasks.length}</span>
      </p>
      <ul className="flex flex-col gap-0.5">
        {tasks.map((task) => (
          <li key={task.id} className="flex gap-2">
            <span aria-hidden="true" className="w-3 shrink-0 text-muted-foreground">{task.status === 'completed' ? '✓' : task.status === 'in_progress' ? '›' : '○'}</span>
            <span className={task.status === 'completed' ? 'text-muted-foreground line-through' : undefined}>{task.status === 'in_progress' && task.active_form ? task.active_form : task.subject}</span>
            <span className="sr-only">({STATUS_WORDS[task.status]})</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function Fold({ summary, children, slot, icon, muted = false }: { summary: React.ReactNode; children: React.ReactNode; slot: string; icon?: React.ReactNode; muted?: boolean }) {
  return (
    <details data-slot={slot} className="group min-w-0 text-[13px]">
      <summary
        className={`flex cursor-pointer list-none items-center gap-1.5 rounded-md py-0.5 hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none [&::-webkit-details-marker]:hidden ${muted ? 'text-muted-foreground' : 'text-foreground/85'}`}
      >
        {icon}
        <span className="min-w-0 truncate">{summary}</span>
        <ChevronRight aria-hidden="true" className="size-3.5 shrink-0 text-muted-foreground transition-transform group-open:rotate-90 motion-reduce:transition-none" />
      </summary>
      <div className="mt-1.5 mb-1 ml-[7px] flex flex-col gap-2 border-l border-border pl-3.5">{children}</div>
    </details>
  );
}

const ICON = 'size-3.5 shrink-0 text-muted-foreground';

function stepIcon(kind: AssistantStep['kind']) {
  if (kind === 'bash') return <SquareTerminal aria-hidden="true" className={ICON} />;
  if (kind === 'view') return <ImageIcon aria-hidden="true" className={ICON} />;
  if (kind === 'agents') return <Users aria-hidden="true" className={ICON} />;
  if (kind === 'task') return <ListChecks aria-hidden="true" className={ICON} />;
  return <FileSearch aria-hidden="true" className={ICON} />;
}

function agentSummary(agent: { description: string; steps: AssistantStep[]; ok?: boolean; stopped?: string | null }, running: boolean): string {
  const did = stepsSummary(agent.steps);
  if (running) return `${agent.description} · ${did || 'Starting'}`;
  return `${agent.description} · ${agent.ok ? did || 'reported' : agent.stopped === 'cost_cap' ? 'stopped: cost cap' : 'did not finish'}`;
}

/** One step: one line, folded; open, its command and output, or the picture it looked at. */
export function StepItem({ step }: { step: AssistantStep }) {
  if (step.kind === 'agents' && step.agents?.length) {
    return (
      <Fold slot="assistant-step" summary={step.label} icon={stepIcon('agents')}>
        {step.agents.map((agent, index) => (
          <Fold key={index} slot="assistant-agent" summary={agentSummary(agent, false)} muted>
            {agent.steps.map((inner, at) => <StepItem key={at} step={inner} />)}
            <p className="text-xs whitespace-pre-wrap break-words text-muted-foreground">{agent.report}</p>
          </Fold>
        ))}
      </Fold>
    );
  }
  const hasDetail = Boolean(step.command || step.output || step.thumbnail);
  if (!hasDetail) {
    return (
      <p data-slot="assistant-step" className={`flex min-w-0 items-center gap-1.5 py-0.5 text-[13px] ${step.ok ? 'text-foreground/85' : 'text-muted-foreground'}`}>
        {stepIcon(step.kind)}
        <span className="min-w-0 truncate">{step.label}</span>
      </p>
    );
  }
  return (
    <Fold slot="assistant-step" summary={step.label} icon={stepIcon(step.kind)} muted={!step.ok}>
      {step.command && <pre className="max-h-40 overflow-auto rounded-md bg-muted px-2.5 py-1.5 font-mono text-xs whitespace-pre-wrap break-all">{step.command}</pre>}
      {step.output && <pre className="max-h-56 overflow-auto rounded-md border border-border px-2.5 py-1.5 font-mono text-xs whitespace-pre-wrap break-all text-muted-foreground">{step.output}</pre>}
      {step.thumbnail && (
        <img src={step.thumbnail} alt={`What it looked at: ${step.label}`} loading="lazy" className="max-h-64 w-auto max-w-full self-start rounded-md border border-border bg-white object-contain" />
      )}
    </Fold>
  );
}

// ── Live: while the answer is being worked out ──────────────

function Spinner() {
  return <span aria-hidden="true" className="size-3.5 shrink-0 animate-spin rounded-full border-[1.5px] border-border border-t-foreground motion-reduce:animate-none" />;
}

/** The growing step list while the assistant works, and the line saying what it is doing now. */
export function LiveSteps({ entries, stage }: { entries: readonly LiveEntry[]; stage: string | null }) {
  const last = entries[entries.length - 1];
  const runningAgents = last?.kind === 'agents';
  const named = new Set(entries.map((entry) => (entry.kind === 'step' ? entry.step.label : entry.label)));
  const now = stage ?? 'Sending your question';
  const line = runningAgents ? null : named.has(now) ? 'Thinking' : now;
  return (
    <div data-slot="assistant-live-steps" className="flex min-w-0 flex-col gap-1">
      {entries.map((entry, index) =>
        entry.kind === 'step' ? (
          <StepItem key={index} step={entry.step} />
        ) : (
          <div key={index} data-slot="assistant-step" className="flex min-w-0 flex-col gap-1">
            <p className="flex items-center gap-1.5 text-[13px] text-foreground/85">
              {index === entries.length - 1 ? <Spinner /> : stepIcon('agents')}
              <span className="min-w-0 truncate">{entry.label}</span>
            </p>
            <div className="ml-[7px] flex flex-col gap-1 border-l border-border pl-3.5">
              {entry.agents.map((agent) => (
                <Fold key={agent.description} slot="assistant-agent" summary={agentSummary(agent, true)} muted>
                  {agent.steps.map((inner, at) => <StepItem key={at} step={inner} />)}
                </Fold>
              ))}
            </div>
          </div>
        ),
      )}
      {line !== null && (
        <div data-slot="assistant-working" className="flex items-center gap-2 py-0.5 text-[13px] text-muted-foreground">
          <Spinner />
          <span className="animate-pulse motion-reduce:animate-none">{line}</span>
        </div>
      )}
    </div>
  );
}

export function AssistantSteps({ steps, plan }: { steps?: AssistantStep[]; plan?: AssistantPlanTask[] }) {
  const shown = steps ?? [];
  if (!shown.length && !plan?.length) return null;
  return (
    <div data-slot="assistant-steps" className="flex flex-col gap-2">
      {shown.length > 0 && (
        <Fold slot="assistant-steps-fold" summary={stepsSummary(shown) || 'Steps'} muted>
          {shown.map((step, index) => <StepItem key={index} step={step} />)}
        </Fold>
      )}
      {plan && <PlanList tasks={plan} />}
    </div>
  );
}
