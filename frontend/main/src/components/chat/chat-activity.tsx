import { useId, useState } from 'react';
import {
  Check,
  ChevronRight,
  CircleAlert,
  ClipboardList,
  FilePen,
  FilePlus,
  FileSearch,
  Image as ImageIcon,
  Lightbulb,
  ListChecks,
  Paperclip,
  SquareTerminal,
  Users,
} from 'lucide-react';

import { ChatMarkdown } from '@/components/chat/chat-markdown';
import { Collapse, GVSpark, Shimmer, Spinner } from '@/components/chat/chat-motion';
import { Dialog, DialogContent, DialogTitle } from '@/components/ui/dialog';
import type { ChatStep, ChatTask } from '@/lib/chat-api';
import {
  activityHeadline,
  activitySummary,
  agentsRunning,
  agentsWords,
  modelLabel,
  thoughtFor,
  type ViewAgent,
  type ViewBlock,
  type ViewStep,
} from '@/lib/chat-messages';
import { cn } from '@/lib/utils';

/**
 * The work behind an answer (#1470), folded the way claude.ai folds it: every run of thinking, tool
 * steps, task-list updates and helpers between two pieces of words is ONE row ("Thought for 7s, ran
 * 2 commands"). While it runs, the row shows the newest thinking line or the running step's own
 * description, shimmering. Opened, it is a timeline (a thin line, a small icon per entry) where
 * thinking and steps alternate in the order they happened; a step opens to its command and output
 * or its picture (click to zoom), helpers to their own steps and report.
 * Read-only: nothing here changes the review.
 */

type PictureUrl = (pictureId: string) => string;

const ROW_BUTTON =
  'group/row flex w-full min-w-0 items-center gap-1.5 rounded-md py-1 text-left text-[13px] text-muted-foreground transition-colors hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none';

function Chevron({ open }: { open: boolean }) {
  return (
    <ChevronRight
      aria-hidden="true"
      className={cn('size-3.5 shrink-0 transition-transform duration-200 ease-out motion-reduce:transition-none', open && 'rotate-90')}
    />
  );
}

/** A step's small icon, by its tool. */
function StepIcon({ step }: { step: ChatStep }) {
  const props = { className: 'size-3.5' };
  switch (step.tool) {
    case 'bash_tool':
      return <SquareTerminal {...props} />;
    case 'view':
      return step.picture_id ? <ImageIcon {...props} /> : <FileSearch {...props} />;
    case 'create_file':
      return <FilePlus {...props} />;
    case 'str_replace':
      return <FilePen {...props} />;
    case 'present_files':
      return <Paperclip {...props} />;
    case 'TaskCreate':
    case 'TaskUpdate':
      return <ListChecks {...props} />;
    case 'Agent':
      return <Users {...props} />;
    default:
      return <ClipboardList {...props} />;
  }
}

/** A dot on the timeline: the entry's small icon, on the page's background so the line passes behind. */
function Dot({ children }: { children: React.ReactNode }) {
  return (
    <span aria-hidden="true" className="relative z-[1] mt-[3px] grid size-5 shrink-0 place-items-center rounded-full bg-background text-muted-foreground">
      {children}
    </span>
  );
}

// ── A picture, and its zoom ─────────────────────────────────

function Picture({ url, label }: { url: string; label: string }) {
  const [zoomed, setZoomed] = useState(false);
  return (
    <>
      <button
        type="button"
        onClick={() => setZoomed(true)}
        aria-label={`Zoom in: ${label}`}
        className="block w-fit max-w-full overflow-hidden rounded-lg border bg-muted/40 transition-opacity hover:opacity-90 focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
      >
        <img src={url} alt={`Picture: ${label}`} loading="lazy" className="max-h-48 w-auto max-w-full object-contain" />
      </button>
      <Dialog open={zoomed} onOpenChange={setZoomed}>
        <DialogContent data-slot="chat-picture-zoom" aria-describedby={undefined} className="max-h-[92vh] w-auto max-w-[min(96vw,1400px)] gap-2 p-3 sm:max-w-[min(96vw,1400px)]">
          <DialogTitle className="pr-8 text-sm font-medium">{label}</DialogTitle>
          <img src={url} alt={`Picture: ${label}`} className="max-h-[80vh] w-auto max-w-full rounded-md object-contain" />
        </DialogContent>
      </Dialog>
    </>
  );
}

// ── One step ────────────────────────────────────────────────

function StepDetail({ step, pictureUrl }: { step: ChatStep; pictureUrl: PictureUrl }) {
  const command = step.command ?? step.path ?? null;
  const [technical, setTechnical] = useState(false);
  const technicalId = useId();
  if (step.tasks?.length) {
    return <div className="pt-1 pb-2"><TaskChecklist tasks={step.tasks} running={false} /></div>;
  }
  return (
    <div className="grid min-w-0 gap-2 pt-1 pb-2">
      {step.picture_id && <Picture url={pictureUrl(step.picture_id)} label={step.label} />}
      {/* #1712: a reviewer reads what was done, not the command: the command and its output wait under
          a closed "Technical details" fold. */}
      {(command || step.output) && (
        <div data-slot="chat-step-technical" className="grid min-w-0 gap-2">
          <button type="button" aria-expanded={technical} aria-controls={technicalId} onClick={() => setTechnical(!technical)} className={cn(ROW_BUTTON, 'w-fit py-0.5 text-xs')}>
            <Chevron open={technical} />
            Technical details
          </button>
          <Collapse open={technical} id={technicalId}>
            <div className="grid min-w-0 gap-2">
              {command && (
                <pre data-slot="chat-step-command" className="overflow-x-auto rounded-md bg-muted/60 px-2.5 py-1.5 font-mono text-[12px] leading-relaxed whitespace-pre-wrap text-foreground [overflow-wrap:anywhere]">
                  {command}
                </pre>
              )}
              {step.output && (
                <pre data-slot="chat-step-output" className="max-h-64 overflow-auto rounded-md border bg-muted/30 px-2.5 py-1.5 font-mono text-[12px] leading-relaxed whitespace-pre-wrap text-muted-foreground [overflow-wrap:anywhere]">
                  {step.output}
                </pre>
              )}
            </div>
          </Collapse>
        </div>
      )}
      {!command && !step.output && !step.picture_id && <p className="text-xs text-muted-foreground">Nothing more to show for this step.</p>}
    </div>
  );
}

function StepEntry({ step, live, pictureUrl }: { step: ChatStep; live: boolean; pictureUrl: PictureUrl }) {
  const [open, setOpen] = useState(false);
  const id = useId();
  const failed = step.ok === false;
  return (
    <li data-slot="chat-entry" data-kind="step" data-tool={step.tool} className="relative flex min-w-0 gap-2.5">
      <Dot>{live ? <Spinner className="size-3" /> : <StepIcon step={step} />}</Dot>
      <div className="min-w-0 flex-1">
        <button type="button" aria-expanded={open} aria-controls={id} onClick={() => setOpen(!open)} className={ROW_BUTTON}>
          <Shimmer active={live} className="min-w-0 truncate">{step.label}</Shimmer>
          {failed && (
            <span className="inline-flex shrink-0 items-center gap-1 text-xs text-destructive">
              <CircleAlert className="size-3" aria-hidden="true" />
              Failed
            </span>
          )}
          <Chevron open={open} />
        </button>
        <Collapse open={open} id={id}>
          <StepDetail step={step} pictureUrl={pictureUrl} />
        </Collapse>
      </div>
    </li>
  );
}

/** The newest task-list update: its line, then the list as it stands now, open in the timeline. */
function TasksEntry({ step, live, tasks, running }: { step: ChatStep; live: boolean; tasks: readonly ChatTask[]; running: boolean }) {
  return (
    <li data-slot="chat-entry" data-kind="tasks" data-tool={step.tool} className="relative flex min-w-0 gap-2.5">
      <Dot>{live ? <Spinner className="size-3" /> : <ListChecks className="size-3.5" />}</Dot>
      <div className="min-w-0 flex-1 py-1">
        <p className="truncate text-[13px] text-muted-foreground"><Shimmer active={live}>{step.label}</Shimmer></p>
        <div className="mt-1.5"><TaskChecklist tasks={tasks} running={running} /></div>
      </div>
    </li>
  );
}

// ── Thinking, inside the timeline ───────────────────────────

function ThinkingEntry({ block }: { block: Extract<ViewBlock, { type: 'thinking' }> }) {
  const title = block.live ? 'Thinking…' : thoughtFor(block.seconds);
  return (
    <li data-slot="chat-entry" data-kind="thinking" className="relative flex min-w-0 gap-2.5">
      <Dot><Lightbulb className="size-3.5" /></Dot>
      <div className="min-w-0 flex-1 py-1">
        <p className="text-[13px] text-muted-foreground"><Shimmer active={block.live}>{title}</Shimmer></p>
        {block.text.trim() && (
          <ChatMarkdown text={block.text} className="mt-1 gap-2 text-[13px] leading-relaxed text-muted-foreground" />
        )}
      </div>
    </li>
  );
}

// ── Helpers ─────────────────────────────────────────────────

/** "Sonnet 5.5" from "claude-sonnet-5-5". */
function shortModel(model: string): string {
  return modelLabel(model, null);
}

function agentStatus(agent: ViewAgent): string {
  if (agent.running) return 'Running';
  switch (agent.stopped) {
    case 'cost_cap':
      return 'Stopped at the cost limit';
    case 'turn_limit':
      return 'Stopped after the maximum number of steps';
    case 'cancelled':
      return 'Stopped';
    case 'error':
      return 'Failed';
    default:
      return agent.ok === false ? 'Failed' : 'Done';
  }
}

function AgentRow({ agent, pictureUrl }: { agent: ViewAgent; pictureUrl: PictureUrl }) {
  const [open, setOpen] = useState(false);
  const id = useId();
  const running = Boolean(agent.running);
  const status = agentStatus(agent);
  const current = running ? agent.steps[agent.steps.length - 1]?.label : null;
  const trouble = !running && (agent.ok === false || Boolean(agent.stopped));
  return (
    <li data-slot="chat-agent" data-status={running ? 'running' : trouble ? 'stopped' : 'done'} className="min-w-0">
      <button type="button" aria-expanded={open} aria-controls={id} onClick={() => setOpen(!open)} className={cn(ROW_BUTTON, 'items-start')}>
        <span className="mt-0.5 grid size-4 shrink-0 place-items-center" aria-hidden="true">
          {running ? <Spinner className="size-3" /> : trouble ? <CircleAlert className="size-3.5" /> : <Check className="size-3.5" />}
        </span>
        <span className="grid min-w-0 flex-1 gap-0.5">
          <span className="truncate text-foreground">{agent.description}</span>
          <span className="truncate text-xs">
            {[shortModel(agent.model), status].join(' · ')}
            {current && <> · <Shimmer active>{current}</Shimmer></>}
          </span>
        </span>
        <span className="mt-0.5"><Chevron open={open} /></span>
      </button>
      <Collapse open={open} id={id}>
        <div className="grid min-w-0 gap-2 pt-1 pb-2 pl-5">
          {agent.steps.length > 0 && (
            <Timeline
              entries={agent.steps.map((step, at) => ({ type: 'step', step, live: running && at === agent.steps.length - 1 && step.ok === null }))}
              pictureUrl={pictureUrl}
            />
          )}
          {agent.report?.trim() ? (
            <div data-slot="chat-agent-report" className="rounded-lg border bg-muted/30 px-3 py-2">
              <p className="mb-1 text-xs font-medium text-muted-foreground">Report</p>
              <ChatMarkdown text={agent.report} className="gap-2 text-[13px] leading-relaxed" />
            </div>
          ) : (
            !running && <p className="text-xs text-muted-foreground">No report.</p>
          )}
        </div>
      </Collapse>
    </li>
  );
}

function AgentsEntry({ step, live, pictureUrl }: { step: ViewStep; live: boolean; pictureUrl: PictureUrl }) {
  const running = agentsRunning(step, live);
  return (
    <li data-slot="chat-entry" data-kind="agents" className="relative flex min-w-0 gap-2.5">
      <Dot>{running ? <Spinner className="size-3" /> : <Users className="size-3.5" />}</Dot>
      <div data-slot="chat-agents" className="min-w-0 flex-1 py-1">
        <p className="text-[13px] text-muted-foreground"><Shimmer active={running}>{agentsWords(step, running)}</Shimmer></p>
        {Boolean(step.agents?.length) && (
          <ul className="mt-1 grid min-w-0 gap-0.5">
            {step.agents!.map((agent) => <AgentRow key={agent.id} agent={agent} pictureUrl={pictureUrl} />)}
          </ul>
        )}
      </div>
    </li>
  );
}

// ── The timeline ────────────────────────────────────────────

type Entry = Extract<ViewBlock, { type: 'thinking' }> | Extract<ViewBlock, { type: 'step' }>;

/** The task list as it stands now, and the entry (by position in the timeline) that shows it. */
export interface CurrentTasks {
  at: number;
  tasks: readonly ChatTask[];
  running: boolean;
}

function Timeline({ entries, pictureUrl, current = null }: { entries: readonly Entry[]; pictureUrl: PictureUrl; current?: CurrentTasks | null }) {
  return (
    <ol
      data-slot="chat-timeline"
      className="relative grid min-w-0 gap-0.5 before:absolute before:top-3 before:bottom-3 before:left-[9.5px] before:w-px before:bg-border"
    >
      {entries.map((entry, at) =>
        entry.type === 'thinking' ? (
          <ThinkingEntry key={`t-${at}`} block={entry} />
        ) : entry.step.tool === 'Agent' ? (
          <AgentsEntry key={entry.step.id || `a-${at}`} step={entry.step} live={entry.live} pictureUrl={pictureUrl} />
        ) : current && current.at === at ? (
          <TasksEntry key={entry.step.id || `k-${at}`} step={entry.step} live={entry.live} tasks={current.tasks} running={current.running} />
        ) : (
          <StepEntry key={entry.step.id || `s-${at}`} step={entry.step} live={entry.live} pictureUrl={pictureUrl} />
        ),
      )}
    </ol>
  );
}

// ── The row ─────────────────────────────────────────────────

/**
 * One activity row. `active` while the answer runs and this is its newest part: the row then says
 * what it is doing now, shimmering; otherwise it sums up what was done. `tasks` is the task list as
 * it stands now, shown open at the row's last task-list update (`tasksAt`, a position in `blocks`).
 */
export function ActivityRow({
  blocks,
  active,
  pictureUrl,
  tasks = null,
  tasksAt = null,
  running = false,
}: {
  blocks: readonly ViewBlock[];
  active: boolean;
  pictureUrl: PictureUrl;
  tasks?: readonly ChatTask[] | null;
  tasksAt?: number | null;
  running?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const id = useId();
  const entries: Entry[] = [];
  let current: CurrentTasks | null = null;
  for (let at = 0; at < blocks.length; at += 1) {
    const block = blocks[at];
    if (block.type === 'text') continue;
    if (at === tasksAt && tasks?.length) current = { at: entries.length, tasks, running };
    entries.push(block);
  }
  const title = active ? activityHeadline(entries) : activitySummary(entries);
  return (
    <div data-slot="chat-activity" data-active={active ? 'true' : 'false'} className="min-w-0">
      <button
        type="button"
        data-slot="chat-activity-toggle"
        aria-expanded={open}
        aria-controls={id}
        onClick={() => setOpen(!open)}
        className={cn(ROW_BUTTON, 'w-fit max-w-full text-sm')}
      >
        {/* #1534: GV's sign leads the work fold; it turns while the work runs. */}
        <GVSpark state={active ? 'working' : 'still'} size={16} />
        <Shimmer active={active} className="min-w-0 truncate">{title}</Shimmer>
        <Chevron open={open} />
      </button>
      <Collapse open={open} id={id}>
        <div className="pt-1.5 pb-1">
          <Timeline entries={entries} pictureUrl={pictureUrl} current={current} />
        </div>
      </Collapse>
    </div>
  );
}

// ── The task list ───────────────────────────────────────────

const TASK_WORDS: Record<ChatTask['status'], string> = { pending: 'to do', in_progress: 'in progress', completed: 'done' };

/**
 * claude.ai's compact progress checklist: a circle to do, a spinner while in progress, a check when
 * done (struck through), with "1 of 3 done" above.
 */
export function TaskChecklist({ tasks, running }: { tasks: readonly ChatTask[]; running: boolean }) {
  if (!tasks.length) return null;
  const done = tasks.filter((task) => task.status === 'completed').length;
  return (
    <div data-slot="chat-tasks" className="min-w-0">
      <p className="mb-1 text-xs text-muted-foreground">{done} of {tasks.length} done</p>
      <ul className="grid gap-1">
        {tasks.map((task) => {
          const working = task.status === 'in_progress';
          const finished = task.status === 'completed';
          return (
            <li key={task.id} data-slot="chat-task" data-status={task.status} className="flex min-w-0 items-start gap-2 text-[13px] leading-snug">
              <span className="mt-px grid size-4 shrink-0 place-items-center" aria-hidden="true">
                {finished ? (
                  <span className="grid size-3.5 place-items-center rounded-full bg-foreground text-background"><Check className="size-2.5" strokeWidth={3} /></span>
                ) : working && running ? (
                  <Spinner />
                ) : (
                  <span className={cn('size-3.5 rounded-full border-[1.5px]', working ? 'border-foreground' : 'border-muted-foreground/60')} />
                )}
              </span>
              <span className={cn('min-w-0 break-words', finished ? 'text-muted-foreground line-through' : 'text-foreground', working && 'font-medium')}>
                {working ? <Shimmer active={running}>{task.active_form || task.subject}</Shimmer> : task.subject}
              </span>
              <span className="sr-only">({TASK_WORDS[task.status]})</span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
