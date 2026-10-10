/**
 * The review assistant's rules (#1129), kept free of React so they can be tested alone.
 *
 * The model writes words; everything else the panel shows is the app's own record, looked up by id:
 * a countertop card shows the row the results table shows, a "what is left" list is the queue's own
 * list. An id the records do not hold is skipped, never filled in, and nothing here can decide.
 */
import { ApiError, type CountertopResult } from '@/api/client';
import type {
  AssistantAction,
  AssistantAnswer,
  AssistantCitation,
  AssistantEvidence,
  AssistantFocus,
  AssistantHistoryTurn,
} from '@/api/assistantTypes';
import type { Finding, Outcome } from '@/data/types';
import { OUTCOME_LABELS } from '@/data/outcomeLabels';
import { architectItemKey, buildQueue, type QueueItem } from '@/lib/needs-you-queue';
import { sortRows } from '@/lib/countertop-results';
import { targetFromFinding, targetFromRow, type ViewerTarget } from '@/lib/drawing-viewer';

/** The records the panel draws from: the same data the Results screen and the queue use. */
export interface AssistantRecords {
  rows: readonly CountertopResult[];
  /** False until the countertop results have loaded; lists then stay out rather than say "none". */
  rowsReady: boolean;
  pagesWithoutCountertop: readonly { page_number: number; reason: string }[];
  rowsNotChecked: readonly { page_number: number; reason: string }[];
  findings: readonly Finding[];
  /** Readiness's blocking finding ids; null while it loads. */
  blocking: ReadonlySet<string> | null;
  /** Rule id → its name in the rulebook, as "Other checks" shows it. Optional: ids fall back to labels. */
  ruleNames?: ReadonlyMap<string, string>;
}

/** What the reviewer is looking at: the drawing, a queue item or a countertop card. */
export interface AssistantContext {
  page_number: number;
  record_id: string | null;
}

export function focusOf(context: AssistantContext | null): AssistantFocus | undefined {
  if (!context) return undefined;
  return context.record_id ? { page_number: context.page_number, record_id: context.record_id } : { page_number: context.page_number };
}

export function sameContext(a: AssistantContext | null, b: AssistantContext | null): boolean {
  return a?.page_number === b?.page_number && (a?.record_id ?? null) === (b?.record_id ?? null);
}

// ── The answer text ─────────────────────────────────────────

/** A chip's target: a countertop row (its own outline), a finding, or just a page. */
export type CiteTarget =
  | { kind: 'row'; page: number; row: CountertopResult }
  | { kind: 'finding'; page: number | null; finding: Finding }
  | { kind: 'page'; page: number };

export type AnswerPart = { kind: 'text'; text: string } | { kind: 'cite'; index: number; label: string; target: CiteTarget };

const MARKER = /\[\[(\d+)\]\]/g;

function rowById(records: Pick<AssistantRecords, 'rows'>, id: string): CountertopResult | undefined {
  return records.rows.find((row) => row.row_id === id) ?? records.rows.find((row) => row.finding_id === id);
}

function findingPage(finding: Finding): number | null {
  return finding.row_location?.page_number ?? finding.shop_evidence?.page ?? finding.arch_evidence?.page ?? null;
}

/**
 * Where a citation points, from the records. A record id the records do not hold gives null (the
 * chip is skipped); a citation with neither an id nor a page gives null too.
 */
export function citeTarget(citation: AssistantCitation, records: Pick<AssistantRecords, 'rows' | 'findings'>): CiteTarget | null {
  if (citation.record_id) {
    const row = rowById(records, citation.record_id);
    if (row) return { kind: 'row', page: citation.page_number ?? row.page_number, row };
    const finding = records.findings.find((f) => f.id === citation.record_id);
    if (finding) {
      const page = citation.page_number ?? findingPage(finding);
      return { kind: 'finding', page, finding };
    }
    return null;
  }
  return citation.page_number !== null ? { kind: 'page', page: citation.page_number } : null;
}

/**
 * The recorded outcome of the record a chip points at, from the app's own rows (never the answer's
 * words): a row's look as the results table shows it, or a finding's outcome. Null for a bare page.
 * Shown inside the chip, so a sentence that ever states the wrong outcome sits beside the right one.
 */
export function citeOutcome(target: CiteTarget): { outcome: Outcome; word: string } | null {
  if (target.kind === 'row') return rowOutcome(target.row);
  if (target.kind === 'finding') return { outcome: target.finding.outcome, word: OUTCOME_LABELS[target.finding.outcome] };
  return null;
}

/** The chip's words: `p4`, or the citation's own label when it names no page. */
function chipLabel(citation: AssistantCitation, target: CiteTarget): string {
  return target.page !== null ? `p${target.page}` : citation.label;
}

/** The answer text cut at its `[[n]]` markers. A marker with no citation, or an unknown id, is dropped. */
export function answerParts(text: string, citations: readonly AssistantCitation[], records: Pick<AssistantRecords, 'rows' | 'findings'>): AnswerPart[] {
  const parts: AnswerPart[] = [];
  let at = 0;
  for (const match of text.matchAll(MARKER)) {
    const start = match.index ?? 0;
    if (start > at) parts.push({ kind: 'text', text: text.slice(at, start) });
    at = start + match[0].length;
    const index = Number(match[1]);
    const citation = citations[index];
    const target = citation ? citeTarget(citation, records) : null;
    if (citation && target) parts.push({ kind: 'cite', index, label: chipLabel(citation, target), target });
  }
  if (at < text.length) parts.push({ kind: 'text', text: text.slice(at) });
  // Two text parts meet where a marker was dropped; join them so a word is never split in two.
  return parts.reduce<AnswerPart[]>((joined, part) => {
    const last = joined[joined.length - 1];
    if (part.kind === 'text' && last?.kind === 'text') joined[joined.length - 1] = { kind: 'text', text: last.text + part.text };
    else joined.push(part);
    return joined;
  }, []);
}

function withoutMarkers(answer: Pick<AssistantAnswer, 'text' | 'citations'>, label: (citation: AssistantCitation) => string): string {
  return answer.text
    .replace(MARKER, (_match, index: string) => {
      const citation = answer.citations[Number(index)];
      return citation ? label(citation) : '';
    })
    .replace(/[ \t]{2,}/g, ' ')
    .replace(/ ([.,;:!?])/g, '$1')
    .trim();
}

/** The answer as plain text, for Copy: each marker becomes its citation's own label ("page 4"). */
export function plainAnswer(answer: Pick<AssistantAnswer, 'text' | 'citations'>): string {
  return withoutMarkers(answer, (citation) => citation.label);
}

/** The answer as the history sends it: each marker becomes the chip's short words ("p4"). */
export function historyAnswer(answer: Pick<AssistantAnswer, 'text' | 'citations'>): string {
  return withoutMarkers(answer, (citation) => (citation.page_number !== null ? `p${citation.page_number}` : citation.label));
}

/**
 * "✓ Matches the records" only when the server's guard checked a model's answer, or the answer was
 * written from the records alone. Never on a refusal, a switched-off assistant or an error.
 */
export function matchesRecords(answer: Pick<AssistantAnswer, 'checked' | 'mode'>): boolean {
  return answer.checked && (answer.mode === 'llm' || answer.mode === 'records_only');
}

// ── History ─────────────────────────────────────────────────

export interface FinishedTurn {
  question: string;
  answer: Pick<AssistantAnswer, 'text' | 'citations'> | null;
}

/** The server takes at most this many characters per history message (`HistoryTurn.text`). */
export const HISTORY_TEXT_MAX = 2000;

/**
 * The last six messages of the earlier turns, as plain text, each cut to the server's limit. A turn
 * with no answer (an error, or stopped) is left out whole, so the model never sees a question that
 * was not answered, and a message with no words is left out (the server refuses an empty one).
 */
export function historyFor(turns: readonly FinishedTurn[], max = 6): AssistantHistoryTurn[] {
  const all: AssistantHistoryTurn[] = turns.flatMap((turn) =>
    turn.answer
      ? [
          { role: 'user' as const, text: turn.question.trim().slice(0, HISTORY_TEXT_MAX) },
          { role: 'assistant' as const, text: historyAnswer(turn.answer).slice(0, HISTORY_TEXT_MAX) },
        ]
      : [],
  );
  return all.filter((message) => message.text.length > 0).slice(-max);
}

// ── Evidence, from the records ──────────────────────────────

/**
 * One line of "what is left before sign-off". A countertop is "Page N" with a short reason; another
 * check is its name with its state, and identical ones (same check, same state) are one line with a
 * count, opening the queue at the first of them.
 */
export interface Blocker {
  /** The queue key it opens (the first item, for a group). */
  key: string;
  title: string;
  /** Page number shown in the number face, when the title is "Page N". */
  page: number | null;
  detail: string;
  outcome: Outcome;
  word: string;
  /** How many queue items this line stands for (more than one only for a group of checks). */
  count: number;
}

/** A reason as one short line: its first sentence, starting with a capital, without the full stop. */
export function shortReason(reason: string): string {
  const first = reason.trim().split(/(?<=[.;!?])\s+/)[0].replace(/[.;]+$/, '');
  return first.charAt(0).toUpperCase() + first.slice(1);
}

/**
 * A row's look, as the results table and the drawing viewer show it: an unchecked row is "Not
 * checked", a decided abstention "Not checkable", anything else its recorded outcome's word.
 */
export function rowOutcome(row: Pick<CountertopResult, 'outcome' | 'needs_decision'>): { outcome: Outcome; word: string } {
  if (!row.outcome) return { outcome: 'NOT_FOUND', word: 'Not checked' };
  if (!row.needs_decision && (row.outcome === 'REVIEW_REQUIRED' || row.outcome === 'NOT_FOUND')) return { outcome: 'NOT_FOUND', word: 'Not checkable' };
  return { outcome: row.outcome, word: OUTCOME_LABELS[row.outcome] };
}

/** A check's name as the "Other checks" list shows it: the rulebook's name, else its own label. */
export function checkName(finding: Pick<Finding, 'check_id' | 'scope_label' | 'name'>, ruleNames?: ReadonlyMap<string, string>): string {
  return ruleNames?.get(finding.check_id) ?? (finding.scope_label && finding.scope_label !== 'Package revision' ? finding.scope_label : finding.name);
}

function blockerOf(item: QueueItem, records: AssistantRecords): Blocker {
  if (item.kind === 'check') {
    const finding = records.findings.find((f) => f.id === item.findingId);
    const outcome = finding?.outcome ?? 'REVIEW_REQUIRED';
    const title = finding ? checkName(finding, records.ruleNames) : item.label;
    return { key: item.key, title, page: null, detail: item.page !== null ? `Page ${item.page}` : 'Whole set', outcome, word: OUTCOME_LABELS[outcome], count: 1 };
  }
  const row = records.rows.find((r) => r.row_id === item.rowId);
  const title = `Page ${item.page}`;
  if (item.kind === 'architect') {
    const outcome = row?.architect?.outcome ?? 'REVIEW_REQUIRED';
    return { key: item.key, title, page: item.page, detail: shortReason(row?.architect?.reason ?? 'Matches the architect? Confirm the pairing'), outcome, word: OUTCOME_LABELS[outcome], count: 1 };
  }
  const look = row ? rowOutcome(row) : { outcome: 'REVIEW_REQUIRED' as Outcome, word: OUTCOME_LABELS.REVIEW_REQUIRED };
  const reason = row?.hold?.reason
    ?? (look.outcome === 'FAIL' ? 'A fail with no decision yet' : look.outcome === 'PASS' ? 'A pass waiting for your decision' : 'Waiting for your decision');
  return { key: item.key, title, page: item.page, detail: shortReason(reason), ...look, count: 1 };
}

/**
 * What still blocks sign-off: the queue's own list, in its order, so each line opens exactly there.
 * Other checks with the same name and state are one line with their count.
 */
export function blockersOf(records: AssistantRecords): Blocker[] {
  const out: Blocker[] = [];
  for (const item of buildQueue(records.rows, records.findings, records.blocking)) {
    const blocker = blockerOf(item, records);
    const same = item.kind === 'check' ? out.find((b) => b.page === null && b.title === blocker.title && b.outcome === blocker.outcome && b.key.startsWith('finding:')) : undefined;
    if (same) {
      same.count += 1;
      same.detail = `${same.count} results`;
    } else out.push(blocker);
  }
  return out;
}

/** The lines shown at most; the rest are counted and left to the queue. */
export const BLOCKERS_SHOWN = 8;

export type ResolvedEvidence =
  | { kind: 'countertop'; key: string; row: CountertopResult; finding: Finding | null }
  | { kind: 'blockers'; key: string; items: Blocker[] }
  | { kind: 'no_countertop_pages'; key: string; items: readonly { page_number: number; reason: string }[] }
  | { kind: 'rows_not_checked'; key: string; items: readonly { page_number: number; reason: string }[] };

/**
 * The evidence the answer names, looked up in the records. An unknown id, a list whose data has not
 * loaded, or an empty list is left out: the panel never draws a record it does not have.
 */
export function resolveEvidence(evidence: readonly AssistantEvidence[], records: AssistantRecords): ResolvedEvidence[] {
  const seen = new Set<string>();
  const out: ResolvedEvidence[] = [];
  for (const item of evidence) {
    const key = item.kind === 'countertop' ? `countertop:${item.record_id}` : item.kind;
    if (seen.has(key)) continue;
    seen.add(key);
    if (item.kind === 'countertop') {
      const row = rowById(records, item.record_id);
      if (!row) continue;
      out.push({ kind: 'countertop', key, row, finding: records.findings.find((f) => f.id === row.finding_id) ?? null });
    } else if (!records.rowsReady) {
      continue;
    } else if (item.kind === 'blockers') {
      if (records.blocking === null) continue;
      const items = blockersOf(records);
      if (items.length > 0) out.push({ kind: 'blockers', key, items });
    } else if (item.kind === 'no_countertop_pages') {
      if (records.pagesWithoutCountertop.length > 0) out.push({ kind: 'no_countertop_pages', key, items: records.pagesWithoutCountertop });
    } else if (item.kind === 'rows_not_checked') {
      if (records.rowsNotChecked.length > 0) out.push({ kind: 'rows_not_checked', key, items: records.rowsNotChecked });
    }
  }
  return out;
}

/**
 * The queue item a countertop row stands for: its own item while its width needs a decision, else
 * its architect item while that needs one. Null when neither waits for the reviewer.
 */
export function rowQueueKey(row: Pick<CountertopResult, 'row_id' | 'needs_decision' | 'architect'>): string | null {
  if (row.needs_decision) return `row:${row.row_id}`;
  if (row.architect?.needs_decision && row.architect.finding_id) return architectItemKey(row.row_id);
  return null;
}

/**
 * Where "Open in queue" opens for a record, only when the queue lists that item: a countertop's own
 * item, its architect item, or another check. Null otherwise, so the queue never opens on a
 * different item than the one named.
 */
export function queueKeyFor(recordId: string, records: Pick<AssistantRecords, 'rows' | 'findings' | 'blocking'>): string | null {
  const byRow = records.rows.find((row) => row.row_id === recordId || row.finding_id === recordId);
  const byArchitect = byRow ? undefined : records.rows.find((row) => row.architect?.finding_id === recordId);
  const key = byRow
    ? rowQueueKey(byRow)
    : byArchitect
      ? architectItemKey(byArchitect.row_id)
      : records.findings.some((f) => f.id === recordId)
        ? `finding:${recordId}`
        : null;
  if (key === null) return null;
  return buildQueue(records.rows, records.findings, records.blocking).some((item) => item.key === key) ? key : null;
}

/** The page a record is on, for an action that cannot open the queue. */
function recordPage(recordId: string, records: Pick<AssistantRecords, 'rows' | 'findings'>): number | null {
  const row = records.rows.find((r) => r.row_id === recordId || r.finding_id === recordId || r.architect?.finding_id === recordId);
  if (row) return row.page_number;
  const finding = records.findings.find((f) => f.id === recordId);
  return finding ? findingPage(finding) : null;
}

export type UsableAction =
  | Extract<AssistantAction, { kind: 'open_page' }>
  | (Extract<AssistantAction, { kind: 'open_queue_item' }> & { queueKey: string });

export const actionKey = (action: UsableAction) => (action.kind === 'open_page' ? `page:${action.page_number}` : action.queueKey);

/**
 * The answer's own navigation buttons. A queue item the queue does not list becomes "Open page N"
 * on the drawing when its page is known, and is left out otherwise; so is an unknown record.
 */
export function usableActions(actions: readonly AssistantAction[], records: Pick<AssistantRecords, 'rows' | 'findings' | 'blocking'>): UsableAction[] {
  const out: UsableAction[] = [];
  for (const action of actions) {
    let usable: UsableAction | null = action.kind === 'open_page' ? action : null;
    if (action.kind === 'open_queue_item') {
      const queueKey = queueKeyFor(action.record_id, records);
      const page = queueKey ? null : recordPage(action.record_id, records);
      usable = queueKey ? { ...action, queueKey } : page !== null ? { kind: 'open_page', page_number: page, label: `Open page ${page}` } : null;
    }
    // Two buttons that do the same thing are one.
    if (usable && !out.some((o) => actionKey(o) === actionKey(usable))) out.push(usable);
  }
  return out;
}

// ── Starters ────────────────────────────────────────────────

export type StarterKind = 'why' | 'left' | 'held' | 'none' | 'ask';

const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;

/**
 * An icon and one muted line for a starter question, from the records only. The question is the
 * server's text; this only recognises a page number or a few plain phrases in it, and says nothing
 * when it cannot tell.
 */
export function starterHint(question: string, records: AssistantRecords): { kind: StarterKind; reason: string | null } {
  const q = question.toLowerCase();
  if (/no countertop/.test(q)) {
    return { kind: 'none', reason: records.rowsReady ? `${plural(records.pagesWithoutCountertop.length, 'page', 'pages')} listed, not blocking` : null };
  }
  if (/not checked/.test(q)) {
    return { kind: 'none', reason: records.rowsReady ? `${plural(records.rowsNotChecked.length, 'row', 'rows')} listed, not checked` : null };
  }
  if (/sign[ -]?off|\bleft\b|remaining/.test(q)) {
    if (!records.rowsReady || records.blocking === null) return { kind: 'left', reason: null };
    const count = buildQueue(records.rows, records.findings, records.blocking).length;
    return { kind: 'left', reason: count === 0 ? 'Nothing blocks sign-off' : `${plural(count, 'item needs', 'items need')} you` };
  }
  const page = /\bpage\s+(\d+)\b/.exec(q);
  if (page) {
    const rows = records.rows.filter((row) => row.page_number === Number(page[1]));
    if (rows.length !== 1) return { kind: 'why', reason: null };
    const row = rows[0];
    const look = rowOutcome(row);
    const held = row.needs_decision && look.outcome !== 'FAIL';
    const waiting = row.needs_decision && look.outcome !== 'REVIEW_REQUIRED';
    return { kind: held ? 'held' : 'why', reason: waiting ? `${look.word} · waiting for you` : look.word };
  }
  return { kind: 'ask', reason: null };
}

// ── Opening a page ──────────────────────────────────────────

/**
 * What "open page N" shows in the drawing viewer: the page's countertop (its outline and the page
 * strip come too), else a finding located on it, else the bare page. A bare page uses the drawing
 * the countertop rows were read from when they all share one; otherwise the viewer says it has no
 * picture rather than this guessing a drawing.
 */
export function pageTarget(page: number, records: Pick<AssistantRecords, 'rows' | 'findings' | 'pagesWithoutCountertop'>): ViewerTarget {
  const row = sortRows(records.rows.filter((r) => r.page_number === page))[0];
  if (row) return targetFromRow(row);
  const finding = records.findings.find((f) => findingPage(f) === page);
  if (finding) return targetFromFinding(finding);
  const versions = new Set(records.rows.flatMap((r) => (r.row_location?.document_version_id ? [r.row_location.document_version_id] : [])));
  const listed = records.pagesWithoutCountertop.some((p) => p.page_number === page);
  return {
    key: `page:${page}`,
    label: `Page ${page}`,
    page,
    documentVersionId: versions.size === 1 ? [...versions][0] : null,
    outline: null,
    findingId: null,
    tone: 'missing',
    glyph: 'NO_APPLICABLE_RULE',
    word: listed ? 'No countertop' : 'No result on this page',
    needsYou: false,
    row: null,
    marks: [],
  };
}

// ── Failures ────────────────────────────────────────────────

/**
 * A failure in plain words. The server's own `error` messages already are; anything technical (a
 * refused request body, an unreadable reply, a parser's text) is said here instead.
 */
export function failureWords(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 422) return 'That question could not be sent. Start a new chat and ask again.';
    if (error.status === 404 && (error.code === 'unreadable_response' || !error.message)) return 'The assistant is not available on this server.';
    if (error.code === 'unreadable_response' || !error.message) return 'The assistant could not answer right now. Try again.';
    return error.message;
  }
  // Only a request that never reached the server (fetch's TypeError) is a connection problem.
  if (error instanceof TypeError) return 'The assistant could not be reached. Check the connection and try again.';
  return 'The answer could not be read. Try again.';
}

/**
 * Where the evidence drawn under an answer already takes the reviewer: a countertop card's page and
 * queue item, the visible "what is left" lines, the listed pages. An answer action going to one of
 * these is the same button twice and is left out.
 */
export function offeredByEvidence(evidence: readonly ResolvedEvidence[], records: Pick<AssistantRecords, 'rows' | 'findings' | 'blocking'>): Set<string> {
  const offered = new Set<string>();
  for (const item of evidence) {
    if (item.kind === 'countertop') {
      offered.add(`page:${item.row.page_number}`);
      const key = queueKeyFor(item.row.row_id, records);
      if (key) offered.add(key);
    } else if (item.kind === 'blockers') {
      for (const blocker of item.items.slice(0, BLOCKERS_SHOWN)) offered.add(blocker.key);
    } else {
      for (const page of item.items) offered.add(`page:${page.page_number}`);
    }
  }
  return offered;
}
