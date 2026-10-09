/**
 * The drawing viewer's arithmetic and data (#1045), kept free of React so it can be tested alone.
 *
 * Geometry: every outline and reading spot is stored in the page's own 0–1 space (`coordinate_space:
 * "stored"`). The viewer draws them in an SVG whose viewBox is that same 0–1 square, laid exactly over
 * the page picture, so they cannot drift from the picture at any zoom. Zoom and pan only move and
 * scale that one box. Nothing here guesses a location: an outline that is not a valid 0–1 polygon is
 * not drawn.
 */
import type { CountertopResult, getFindingChain } from '@/api/client';
import type { Finding, Outcome } from '@/data/types';
import { OUTCOME_LABELS } from '@/data/outcomeLabels';
import { drawingPoints } from '@/components/output/reviewerResults';
import { bucketOf, sortRows } from '@/lib/countertop-results';

export type Point = [number, number];
export interface Size { w: number; h: number }
export interface Rect { x: number; y: number; w: number; h: number }
/** Screen pixels per picture pixel, and where the picture's top-left corner sits in the viewport. */
export interface View { scale: number; x: number; y: number }

type Chain = Awaited<ReturnType<typeof getFindingChain>>;
type RowLocation = NonNullable<CountertopResult['row_location']>;

// ── Geometry ────────────────────────────────────────────────

/** A stored 0–1 polygon as points, or null when it is not a valid outline (never a substitute). */
export function outlineOf(polygon: readonly (readonly (string | number)[])[] | null | undefined): Point[] | null {
  if (!polygon) return null;
  try {
    return drawingPoints(polygon, 1, 1);
  } catch {
    return null;
  }
}

export function boundsOf(points: readonly Point[]): Rect {
  const xs = points.map((p) => p[0]);
  const ys = points.map((p) => p[1]);
  const x = Math.min(...xs);
  const y = Math.min(...ys);
  return { x, y, w: Math.max(...xs) - x, h: Math.max(...ys) - y };
}

export function svgPoints(points: readonly Point[]): string {
  return points.map((p) => `${p[0]},${p[1]}`).join(' ');
}

const MARGIN = 16;

/** The whole page in the viewport, centred. */
export function fitView(page: Size, box: Size): View {
  const scale = Math.max(Math.min((box.w - 2 * MARGIN) / page.w, (box.h - 2 * MARGIN) / page.h), 1e-4);
  return { scale, x: (box.w - page.w * scale) / 2, y: (box.h - page.h * scale) / 2 };
}

/** How far a reviewer may zoom: from half the fitted size to three screen pixels per picture pixel. */
export function scaleLimits(page: Size, box: Size): { min: number; max: number } {
  const fit = fitView(page, box).scale;
  return { min: fit / 2, max: Math.max(3, fit * 2) };
}

/** Zoom by `factor`, keeping the picture point under `anchor` (viewport pixels) where it is. */
export function zoomAt(view: View, factor: number, anchor: { x: number; y: number }, limits: { min: number; max: number }): View {
  const scale = Math.min(Math.max(view.scale * factor, limits.min), limits.max);
  const k = scale / view.scale;
  return { scale, x: anchor.x - (anchor.x - view.x) * k, y: anchor.y - (anchor.y - view.y) * k };
}

/** Centre a 0–1 rectangle and zoom so it fills about `fill` of the viewport. */
export function focusView(rect: Rect, page: Size, box: Size, limits: { min: number; max: number }, fill = 0.6): View {
  const w = Math.max(rect.w * page.w, 1);
  const h = Math.max(rect.h * page.h, 1);
  const scale = Math.min(Math.max(Math.min((box.w * fill) / w, (box.h * fill) / h), limits.min), limits.max);
  const cx = (rect.x + rect.w / 2) * page.w;
  const cy = (rect.y + rect.h / 2) * page.h;
  return { scale, x: box.w / 2 - cx * scale, y: box.h / 2 - cy * scale };
}

/** Where a 0–1 page point lands in the viewport. */
export function toScreen(view: View, page: Size, point: Point): Point {
  return [view.x + point[0] * page.w * view.scale, view.y + point[1] * page.h * view.scale];
}

// ── What is being shown ─────────────────────────────────────

export type Tone = 'pass' | 'fail' | 'review' | 'missing';

/**
 * Something else drawn in grey beside a target (#1085): the architect's dimension it was compared
 * with, or a span offered for pairing. Only a stored location is drawn; `outline` null means "not
 * outlined", and the viewer says so instead of guessing a box.
 */
export interface Mark {
  key: string;
  label: string;
  page: number | null;
  documentVersionId: string | null;
  outline: Point[] | null;
}

/** One thing the viewer can point at: a countertop row, or a finding with a location. */
export interface ViewerTarget {
  key: string;
  label: string;
  page: number | null;
  documentVersionId: string | null;
  outline: Point[] | null;
  findingId: string | null;
  tone: Tone;
  glyph: Outcome;
  word: string;
  /** Still waiting for the reviewer, whatever was recorded (shown beside the result, never instead of it). */
  needsYou: boolean;
  row: CountertopResult | null;
  /** Grey marks beside the outline: the architect's compared dimensions (#1085). */
  marks: Mark[];
}

/** The architect's compared dimensions of a row, as grey marks (#1085). Exact API text in labels. */
export function architectMarks(row: Pick<CountertopResult, 'row_id' | 'architect'>): Mark[] {
  return (row.architect?.compared ?? []).map((pair, index) => {
    const location = pair.architect_location ?? null;
    const what = pair.kind === 'overall' ? 'the overall' : `piece ${pair.vendor_piece ?? '?'}`;
    return {
      key: `architect:${row.row_id}:${index}`,
      label: `Architect's drawing says ${pair.architect_display ?? '—'} (${what})`,
      page: location?.page_number ?? null,
      documentVersionId: location?.document_version_id ?? null,
      outline: outlineOf(location?.polygon),
    };
  });
}

const OUTCOME_LOOK: Record<Outcome, Tone> = {
  PASS: 'pass',
  FAIL: 'fail',
  REVIEW_REQUIRED: 'review',
  NOT_FOUND: 'missing',
  NO_APPLICABLE_RULE: 'missing',
};

/**
 * How a countertop row looks, exactly as the results table's result cell shows it: the recorded
 * result in its own words; a decided abstention as "Not checkable"; an unchecked row as "Not
 * checked". "Needs you" is separate (`needsYou`), shown beside the result, never instead of it.
 */
function rowLook(row: Pick<CountertopResult, 'outcome' | 'needs_decision'>): Pick<ViewerTarget, 'tone' | 'glyph' | 'word'> {
  if (!row.outcome) return { tone: 'missing', glyph: 'NOT_FOUND', word: 'Not checked' };
  if (!row.needs_decision && (row.outcome === 'REVIEW_REQUIRED' || row.outcome === 'NOT_FOUND')) {
    return { tone: 'missing', glyph: 'NOT_FOUND', word: 'Not checkable' };
  }
  return { tone: OUTCOME_LOOK[row.outcome], glyph: row.outcome, word: OUTCOME_LABELS[row.outcome] };
}

/** A span offered for pairing, as a grey mark (#1085). Only a stored location is drawn. */
export function spanMark(span: { candidate_id: string; printed: string; location?: RowLocation | null }): Mark {
  return {
    key: `span:${span.candidate_id}`,
    label: `Architect's ${span.printed}`,
    page: span.location?.page_number ?? null,
    documentVersionId: span.location?.document_version_id ?? null,
    outline: outlineOf(span.location?.polygon),
  };
}

export function targetFromRow(row: CountertopResult): ViewerTarget {
  return {
    key: row.row_id,
    label: row.label,
    page: row.row_location?.page_number ?? row.page_number,
    documentVersionId: row.row_location?.document_version_id ?? null,
    outline: outlineOf(row.row_location?.polygon),
    findingId: row.finding_id,
    ...rowLook(row),
    needsYou: bucketOf(row) === 'needs-you',
    row,
    marks: architectMarks(row),
  };
}

/**
 * A countertop seen as its architect check (#1085), for the queue's "Matches the architect?" item:
 * the row's outline and marks, but the look of the architect result, never the width's. A pairing
 * that waits for the reviewer looks like "Needs your decision".
 */
export function targetFromArchitect(row: CountertopResult): ViewerTarget {
  const base = targetFromRow(row);
  const architect = row.architect ?? null;
  const waiting = architect?.needs_decision && architect.outcome === 'REVIEW_REQUIRED';
  const look = !architect?.outcome || waiting
    ? { tone: 'review' as Tone, glyph: 'REVIEW_REQUIRED' as Outcome, word: OUTCOME_LABELS.REVIEW_REQUIRED }
    : rowLook({ outcome: architect.outcome, needs_decision: architect.needs_decision });
  return { ...base, key: `architect:${row.row_id}`, findingId: architect?.finding_id ?? null, ...look, needsYou: Boolean(architect?.needs_decision) };
}

/** A finding: its row outline, or else the location of its shop (then architect) reading. */
export function targetFromFinding(finding: Pick<Finding, 'id' | 'name' | 'scope_label' | 'outcome' | 'row_location' | 'shop_evidence' | 'arch_evidence'>): ViewerTarget {
  const evidence = finding.shop_evidence ?? finding.arch_evidence ?? null;
  const location: Pick<RowLocation, 'page_number' | 'document_version_id' | 'polygon'> | null = finding.row_location
    ?? (evidence ? { page_number: evidence.page, document_version_id: evidence.document_version_id, polygon: evidence.polygon.map((p) => p.map(String)) } : null);
  return {
    key: finding.id,
    label: finding.scope_label ?? finding.name,
    page: location?.page_number ?? null,
    documentVersionId: location?.document_version_id ?? null,
    outline: outlineOf(location?.polygon),
    findingId: finding.id,
    tone: OUTCOME_LOOK[finding.outcome],
    glyph: finding.outcome,
    word: OUTCOME_LABELS[finding.outcome],
    needsYou: false,
    row: null,
    marks: [],
  };
}

export interface StripPage {
  page: number;
  documentVersionId: string;
  targets: ViewerTarget[];
}

/**
 * The pages that have a located countertop, in page order, each with its countertops in the results
 * table's order (needs you first). The current target's page is included even when no row is on it.
 */
export function pagesOf(rows: readonly CountertopResult[], current: ViewerTarget | null): StripPage[] {
  const pages = new Map<string, StripPage>();
  for (const row of sortRows(rows)) {
    const target = targetFromRow(row);
    if (target.page === null || target.documentVersionId === null) continue;
    const key = `${target.documentVersionId}:${target.page}`;
    const entry = pages.get(key) ?? { page: target.page, documentVersionId: target.documentVersionId, targets: [] };
    entry.targets.push(target);
    pages.set(key, entry);
  }
  if (current && current.page !== null && current.documentVersionId !== null) {
    const key = `${current.documentVersionId}:${current.page}`;
    if (!pages.has(key)) pages.set(key, { page: current.page, documentVersionId: current.documentVersionId, targets: [current] });
  }
  return [...pages.values()].sort((a, b) => a.page - b.page || a.documentVersionId.localeCompare(b.documentVersionId));
}

export function samePage(a: Pick<ViewerTarget, 'page' | 'documentVersionId'>, b: Pick<ViewerTarget, 'page' | 'documentVersionId'>): boolean {
  return a.page !== null && a.page === b.page && a.documentVersionId === b.documentVersionId;
}

// ── The readings behind a result ────────────────────────────

/** One stored reading with its crop: what it is, its exact value, and where it was read. */
export interface Reading {
  name: string;
  label: string;
  value: string;
  role: string;
  status: string;
  observationId: string;
  page: number;
  documentVersionId: string;
  spot: Point[] | null;
}

/**
 * Every reading of a finding that has a stored crop, in the chain's order. The value is the
 * engine's own exact text (the trace's rendering, else the stored rational), never a float.
 */
export function readingsOf(chain: Pick<Chain, 'operands' | 'trace'>, row?: Pick<CountertopResult, 'pieces'> | null): Reading[] {
  const traced = new Map(chain.trace.kind === 'calculation' ? chain.trace.operands.map((o) => [o.name, o.value]) : []);
  return (chain.operands ?? []).flatMap((operand) => {
    const evidence = operand.evidence;
    if (!evidence) return [];
    const exact = traced.get(operand.name)
      ?? (operand.denominator === '1' ? `${operand.numerator} ${operand.unit}` : `${operand.numerator}/${operand.denominator} ${operand.unit}`);
    return [{
      name: operand.name,
      label: readingLabel(operand.name, row),
      value: inchMarks(exact),
      role: evidence.document_role.toUpperCase(),
      status: operand.evidence_status,
      observationId: evidence.canonical_observation_id,
      page: evidence.page_index + 1,
      documentVersionId: evidence.document_version_id,
      spot: outlineOf(evidence.polygon),
    }];
  });
}

const PIECE = /^piece_widths\[(\d+)\]$/;
const KIND_WORD: Record<string, string> = { filler: 'Filler', appliance_space: 'Appliance space', cabinet: 'Cabinet', equal_cabinets: 'Cabinet' };

function readingLabel(name: string, row?: Pick<CountertopResult, 'pieces'> | null): string {
  if (name === 'countertop_width') return 'Printed overall';
  const piece = PIECE.exec(name);
  if (piece) {
    const index = Number(piece[1]);
    const kind = row?.pieces.find((p) => p.index === index)?.kind;
    return `${(kind && KIND_WORD[kind]) ?? 'Piece'} ${index + 1}`;
  }
  const words = name.replace(/[_.]+/g, ' ').trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** `30 1/2 in` → `30 1/2"`: the same exact text, in the units the drawings use. */
export function inchMarks(value: string): string {
  return value.replace(/ in\b/g, '"');
}

/** One line for a result with no crops, and the reason behind the "?". */
export function noCropsReason(chain: Pick<Chain, 'operands' | 'trace'> | null, row: Pick<CountertopResult, 'hold' | 'finding_id'> | null): { line: string; why: string } {
  if (row && row.finding_id === null) {
    return { line: 'Not checked yet', why: 'This countertop has no recorded result yet, so there are no readings to show. Run the checks from Measurements.' };
  }
  if (chain?.trace.kind === 'abstention') {
    return {
      line: 'The check did not run',
      why: `No reading was used, so there is nothing to crop. ${row?.hold?.reason ?? chain.trace.reason ?? 'The reason is in the result.'}`,
    };
  }
  if (chain && (chain.operands ?? []).length > 0) {
    return { line: 'No drawing crops', why: 'This result uses values that were typed or set, not read from the drawing, so no crop is expected.' };
  }
  return { line: 'No drawing crops', why: 'A crop appears only when a confirmed drawing reading has a stored location.' };
}
