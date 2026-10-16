import { ChevronRight } from 'lucide-react';

import type { AssistantPlanTask, AssistantStep } from '@/api/assistantTypes';
import { stepsSummary } from '@/lib/assistant-steps';

/**
 * The tools the assistant used for one answer, shown like claude.ai (owner, 2026-10-16): one
 * folded line ("Ran 4 commands, viewed 1 picture"), and inside it each command with its output,
 * each picture, the results it read, and each helper agent with its own folded steps. Its task
 * list shows as "Progress". Read-only: nothing here changes the review.
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

function Fold({ summary, children, slot }: { summary: string; children: React.ReactNode; slot: string }) {
  return (
    <details data-slot={slot} className="group text-xs">
      <summary className="flex cursor-pointer list-none items-center gap-1 text-muted-foreground hover:text-foreground [&::-webkit-details-marker]:hidden">
        <ChevronRight aria-hidden="true" className="size-3.5 shrink-0 transition-transform group-open:rotate-90 motion-reduce:transition-none" />
        <span className="min-w-0 truncate">{summary}</span>
      </summary>
      <div className="mt-1.5 flex flex-col gap-2 border-l border-border pl-3">{children}</div>
    </details>
  );
}

function StepItem({ step }: { step: AssistantStep }) {
  if (step.kind === 'agents' && step.agents?.length) {
    return (
      <div data-slot="assistant-step" className="flex flex-col gap-1.5">
        <p className="text-foreground">{step.label}</p>
        {step.agents.map((agent, index) => (
          <Fold
            key={index}
            slot="assistant-agent"
            summary={`${agent.description} · ${agent.ok ? stepsSummary(agent.steps) || 'reported' : agent.stopped === 'cost_cap' ? 'stopped: cost cap' : 'did not finish'}`}
          >
            {agent.steps.map((inner, at) => <StepItem key={at} step={inner} />)}
            <p className="whitespace-pre-wrap break-words text-muted-foreground">{agent.report}</p>
          </Fold>
        ))}
      </div>
    );
  }
  if (step.kind === 'task') {
    return <p data-slot="assistant-step" className="text-muted-foreground">{step.label}</p>;
  }
  return (
    <div data-slot="assistant-step" className="flex min-w-0 flex-col gap-1">
      <p className={step.ok ? 'text-foreground' : 'text-muted-foreground'}>{step.label}</p>
      {step.command && <pre className="max-h-40 overflow-auto rounded bg-muted px-2 py-1 font-mono text-xs whitespace-pre-wrap break-all">{step.command}</pre>}
      {step.output && <pre className="max-h-48 overflow-auto rounded border border-border px-2 py-1 font-mono text-xs whitespace-pre-wrap break-all text-muted-foreground">{step.output}</pre>}
    </div>
  );
}

export function AssistantSteps({ steps, plan }: { steps?: AssistantStep[]; plan?: AssistantPlanTask[] }) {
  const shown = steps ?? [];
  if (!shown.length && !plan?.length) return null;
  return (
    <div data-slot="assistant-steps" className="flex flex-col gap-2">
      {shown.length > 0 && (
        <Fold slot="assistant-steps-fold" summary={stepsSummary(shown) || 'Steps'}>
          {shown.map((step, index) => <StepItem key={index} step={step} />)}
        </Fold>
      )}
      {plan && <PlanList tasks={plan} />}
    </div>
  );
}
