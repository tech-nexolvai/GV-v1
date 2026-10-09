import { toast } from 'sonner';

import type { CountertopResult } from '@/api/client';
import type { AssistantAnswer, AssistantInfo } from '@/api/assistantTypes';
import type { Finding } from '@/data/types';
import type { AssistantRecords } from '@/lib/assistant';
import { AssistantPanel, type AssistantApi, type AssistantInfoState, type AssistantNavigation, type AssistantTurn } from '@/components/assistant/assistant-panel';

/* The review assistant (#1129) in each state. Every record and answer here is made up. */

const value = (numerator: string, denominator: string, display: string) => ({ numerator, denominator, display });

function sampleRow(id: string, page: number, extra: Partial<CountertopResult>): CountertopResult {
  return {
    finding_id: `f-${id}`, row_id: id, page_number: page, label: `Sample countertop ${page}`,
    row_location: null, outcome: 'PASS', needs_decision: false,
    printed_overall: value('48', '1', '48"'), pieces: [], field_cut_per_end: null, field_cut_count: 0,
    expected_total: value('48', '1', '48"'), delta: value('0', '1', '0"'),
    hold: null, reviewer_decision: null,
    wall_layout: { config: 'back_left', label: 'wall on the left, open end on the right', source: 'reviewer' },
    agreement: { both_readers_agreed_on_row: true, code_clue_used: false, values_agreed: [] },
    ...extra,
  };
}

const ROWS: CountertopResult[] = [
  sampleRow('sample-4', 4, {
    outcome: 'FAIL', needs_decision: true,
    printed_overall: value('169', '2', '84 1/2"'), expected_total: value('691', '8', '86 3/8"'), delta: value('-15', '8', '-1 7/8"'),
  }),
  sampleRow('sample-7', 7, { outcome: 'REVIEW_REQUIRED', needs_decision: true, hold: { code: 'sample', reason: 'The two readers read the overall differently' }, expected_total: null, delta: null }),
  sampleRow('sample-10', 10, { outcome: 'FAIL', needs_decision: true, printed_overall: value('110', '1', '110"'), expected_total: value('215', '2', '107 1/2"'), delta: value('5', '2', '2 1/2"') }),
  sampleRow('sample-3', 3, {}),
];

const FINDINGS: Finding[] = ROWS.map((row) => ({
  id: row.finding_id ?? row.row_id, check_id: 'SAMPLE-WIDTH', name: 'Countertop width', outcome: row.outcome ?? 'NOT_FOUND', severity: 'MAJOR', reviewer_action: null, scope_label: row.label,
}));

const RECORDS: AssistantRecords = {
  rows: ROWS,
  rowsReady: true,
  pagesWithoutCountertop: [{ page_number: 2, reason: 'Wall cabinets only, no countertop line' }],
  rowsNotChecked: [{ page_number: 9, reason: 'A second countertop row on the sheet' }],
  findings: FINDINGS,
  blocking: new Set(['f-sample-4', 'f-sample-7', 'f-sample-10']),
};

const INFO: AssistantInfo = {
  enabled: true,
  model_label: 'Claude Sonnet',
  keeps_no_data: true,
  starters: ['Why did page 4 fail?', 'What is left before sign-off?', 'Why does page 7 need me?', 'Which pages have no countertop?'],
};
const READY: AssistantInfoState = { status: 'ready', info: INFO };

const PAGE_4: AssistantAnswer = {
  text: 'The countertop on [[0]] is printed 84 1/2", but the cabinets and fillers under it need 86 3/8". It is 1 7/8" short.',
  citations: [{ kind: 'countertop', page_number: 4, record_id: 'sample-4', label: 'page 4' }],
  evidence: [{ kind: 'countertop', record_id: 'sample-4' }],
  actions: [],
  suggestions: ['What is left before sign-off?', 'Why does page 7 need me?'],
  checked: true,
  mode: 'llm',
  model_id: 'sample-model',
  sources: ['Check result for page 4, latest run', 'Rule: countertop length against cabinets', 'Your wall answer for page 4'],
};

const LEFT: AssistantAnswer = {
  text: 'Three countertops need your decision before you can sign off. [[0]] and [[1]] are listed only.',
  citations: [
    { kind: 'page', page_number: 2, record_id: null, label: 'page 2' },
    { kind: 'page', page_number: 9, record_id: null, label: 'page 9' },
  ],
  evidence: [{ kind: 'blockers' }, { kind: 'no_countertop_pages' }],
  actions: [],
  suggestions: ['Why does page 7 need me?'],
  checked: true,
  mode: 'records_only',
  model_id: null,
  sources: ['Readiness: 3 items block sign-off', 'Pages with no countertop: 1'],
};

const REFUSED: AssistantAnswer = {
  text: 'I can’t record decisions. Pass or fail comes from the checks, and you decide in the queue with your own click.',
  citations: [],
  evidence: [],
  actions: [{ kind: 'open_queue_item', record_id: 'sample-7', label: 'Open page 7 in the queue' }],
  suggestions: ['Why does page 7 need me?'],
  checked: false,
  mode: 'refused',
  model_id: null,
  sources: [],
};

function turn(id: string, question: string, extra: Partial<AssistantTurn>): AssistantTurn {
  return { id, question, context: null, status: 'done', stage: null, answer: null, error: null, reveal: false, ...extra };
}

/** Answers made up on the spot, so the specimens can be tried. Nothing leaves the page. */
const SAMPLE_API: AssistantApi = {
  info: async () => INFO,
  stream: (_project, _package, body, handlers, signal) =>
    new Promise((resolve, reject) => {
      const steps = [
        () => handlers.onStage?.({ id: 'records', label: 'Reading this review’s records' }),
        () => handlers.onStage?.({ id: 'guard', label: 'Checking every number against the records' }),
        () => resolve(/sign[ -]?off|left/i.test(body.question) ? LEFT : /page 7|passed|mark/i.test(body.question) ? REFUSED : PAGE_4),
      ];
      const timers = steps.map((step, index) => window.setTimeout(step, 650 * (index + 1)));
      signal?.addEventListener('abort', () => {
        timers.forEach((timer) => window.clearTimeout(timer));
        reject(new DOMException('Stopped', 'AbortError'));
      });
    }),
};

const NAV: AssistantNavigation = {
  openPage: (page) => toast(`In a review this opens page ${page} on the drawing.`),
  showRow: (row) => toast(`In a review this shows page ${row.page_number}’s countertop on the drawing.`),
  showFinding: (finding) => toast(`In a review this shows ${finding.name} on the drawing.`),
  openQueue: (key) => toast(`In a review this opens the queue at ${key.split(':')[0] === 'row' ? 'that countertop' : 'that item'}.`),
};

const STATES: { name: string; turns: AssistantTurn[]; context?: { page_number: number; record_id: string } }[] = [
  { name: 'Empty: starter questions from the records', turns: [] },
  {
    name: 'Working: the server’s current stage',
    turns: [turn('w', 'Why did page 4 fail?', { status: 'working', stage: 'Checking every number against the records', context: { page_number: 4, record_id: 'sample-4' } })],
    context: { page_number: 4, record_id: 'sample-4' },
  },
  {
    name: 'Answer with a countertop card',
    turns: [turn('a', 'Why did page 4 fail?', { answer: PAGE_4, context: { page_number: 4, record_id: 'sample-4' } })],
    context: { page_number: 4, record_id: 'sample-4' },
  },
  { name: 'What is left before sign-off', turns: [turn('b', 'What is left before sign-off?', { answer: LEFT })] },
  { name: 'Refusal: no decisions from the panel', turns: [turn('r', 'Mark page 7 as passed', { answer: REFUSED })] },
  {
    name: 'Error, in plain words',
    turns: [turn('e', 'Why did page 10 fail?', { status: 'error', error: 'The model is busy right now. Try again in a minute.' })],
  },
];

export function AssistantSection() {
  return (
    <section id="assistant" aria-labelledby="assistant-title" className="scroll-mt-20">
      <h2 id="assistant-title" className="text-xl font-semibold tracking-tight">
        Assistant
      </h2>
      <p className="mt-1 max-w-prose text-sm text-muted-foreground">
        The review assistant docked beside Results: answers from the review’s records, page chips that open the drawing,
        evidence drawn from the records by id, and nothing that records a decision. Each panel can be tried; its answers are made up.
      </p>
      <div className="mt-5 grid gap-6 xl:grid-cols-2">
        {STATES.map((state, index) => (
          <figure key={state.name} className="flex min-w-0 flex-col gap-2">
            <figcaption className="text-xs text-muted-foreground">{state.name}</figcaption>
            <div className="h-[640px] min-w-0 overflow-hidden rounded-xl border">
              <AssistantPanel
                id={`ui-kit-assistant-${index}`}
                projectId="sample-project"
                packageId="sample-package"
                setName="Sample kitchen set"
                open
                autoFocus={false}
                layout="inline"
                records={RECORDS}
                context={state.context ?? null}
                nav={NAV}
                onClose={() => toast('In a review this closes the assistant.')}
                api={SAMPLE_API}
                initialInfo={READY}
                initialTurns={state.turns}
              />
            </div>
          </figure>
        ))}
      </div>
    </section>
  );
}
