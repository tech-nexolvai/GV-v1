/**
 * Geometry for the countertop picture (#P4), kept apart from the SVG so it can be tested.
 *
 * Follows the shared spec in the vault ("V1 backend - everything left (Codex)", section 4), which the
 * signed PDF report draws too, so the two pictures agree.
 *
 * **Labels are never computed.** Every number written on the picture is the API's exact `display`
 * text. Numbers are converted to floats only to place boxes, after each one is checked to be an
 * exact integer fraction; a value that is not one stops the drawing (`refused`), because a picture
 * built from a malformed width would put boxes at `NaN` and still look like a picture.
 *
 * **To scale only when every width is known.** One missing piece means its width is unknown, so the
 * whole strip is drawn schematically (equal boxes) and says so (`toScale: false`); it never invents
 * a width to keep the picture proportional.
 */
import type { CountertopResult, ExactValue } from '@/api/client';
import { formatDelta, wallsOf } from './countertop-results';

export type PieceKind = 'filler' | 'cabinet' | 'appliance' | 'other';

export interface StripPiece {
  index: number;
  /** Left edge and width, in drawing units (0 … `width`). */
  x: number;
  w: number;
  /** The API's exact text, e.g. `13 1/8"`; null when the piece was not read. */
  label: string | null;
  kind: PieceKind;
  source: CountertopResult['pieces'][number]['source'];
  /** Both readers agreed on this piece (from the row's agreement facts); null when unknown. */
  agreed: boolean | null;
}

export interface StripSpan {
  x: number;
  w: number;
  label: string;
}

export type StripLayout =
  | { status: 'refused'; reason: string }
  | {
      status: 'drawn';
      width: number;
      toScale: boolean;
      pieces: StripPiece[];
      /** The stone as printed, drawn from the left wall face. */
      printed: StripSpan | null;
      /** Pieces plus field cut, drawn from the left wall face. */
      needed: StripSpan | null;
      /** Field-cut caps, only at ends that have a wall. */
      caps: { left: StripSpan | null; right: StripSpan | null };
      walls: { back: boolean; left: boolean; right: boolean } | null;
      difference: { text: string; sign: -1 | 0 | 1 } | null;
      held: { code: string; reason: string } | null;
      /** One sentence for screen readers and the SVG title. */
      summary: string;
    };

const INTEGER = /^-?\d+$/;

/** The value in inches for placement only, or null when it is not an exact integer fraction. */
export function inchesOf(value: ExactValue | null | undefined): number | null {
  if (!value || !INTEGER.test(value.numerator) || !INTEGER.test(value.denominator)) return null;
  const denominator = BigInt(value.denominator);
  if (denominator === 0n) return null;
  return Number(BigInt(value.numerator)) / Number(denominator);
}

function kindOf(kind: string | null | undefined): PieceKind {
  if (kind === 'filler') return 'filler';
  if (kind === 'appliance_space') return 'appliance';
  if (kind === 'cabinet' || kind === 'equal_cabinets') return 'cabinet';
  return 'other';
}

/** Smallest drawn width for a cap, so a 1" field cut on a 140" run is still visible. */
const MIN_CAP = 6;

export function stripLayout(row: CountertopResult, width: number): StripLayout {
  if (row.pieces.length === 0) return { status: 'refused', reason: 'Pieces not read' };

  const values = row.pieces.map((p) => (p.value === null ? null : inchesOf(p.value)));
  if (row.pieces.some((p, i) => p.value !== null && (values[i] === null || (values[i] as number) <= 0))) {
    return { status: 'refused', reason: 'A width is not an exact positive number' };
  }
  const printedIn = row.printed_overall ? inchesOf(row.printed_overall) : null;
  const neededIn = row.expected_total ? inchesOf(row.expected_total) : null;
  const capIn = row.field_cut_per_end ? inchesOf(row.field_cut_per_end) : null;
  if ((row.printed_overall && printedIn === null) || (row.expected_total && neededIn === null) || (row.field_cut_per_end && capIn === null)) {
    return { status: 'refused', reason: 'A total is not an exact number' };
  }

  const walls = wallsOf(row.wall_layout.config);
  const capLeft = Boolean(walls?.left && capIn !== null && capIn > 0);
  const capRight = Boolean(walls?.right && capIn !== null && capIn > 0);
  const toScale = values.every((v) => v !== null);

  const pieces: StripPiece[] = [];
  let printed: StripSpan | null = null;
  let needed: StripSpan | null = null;
  const caps: { left: StripSpan | null; right: StripSpan | null } = { left: null, right: null };
  const capText = row.field_cut_per_end ? `+${row.field_cut_per_end.display}` : '';

  if (toScale) {
    const piecesIn = (values as number[]).reduce((sum, v) => sum + v, 0);
    const capsIn = (capLeft ? capIn! : 0) + (capRight ? capIn! : 0);
    const totalIn = Math.max(piecesIn + capsIn, printedIn ?? 0, neededIn ?? 0);
    const scale = width / totalIn;
    const capW = capIn !== null ? Math.max(capIn * scale, MIN_CAP) : 0;
    let x = 0;
    if (capLeft) {
      caps.left = { x, w: capW, label: capText };
      x += capW;
    }
    row.pieces.forEach((piece, i) => {
      const w = (values[i] as number) * scale;
      pieces.push({ index: piece.index, x, w, label: piece.value?.display ?? null, kind: kindOf(piece.kind), source: piece.source, agreed: row.agreement.values_agreed[i + 1] ?? null });
      x += w;
    });
    if (capRight) caps.right = { x, w: capW, label: capText };
    if (printedIn !== null && row.printed_overall) printed = { x: 0, w: printedIn * scale, label: row.printed_overall.display };
    if (neededIn !== null && row.expected_total) needed = { x: 0, w: neededIn * scale, label: row.expected_total.display };
  } else {
    // Schematic: equal boxes, caps at a fixed size, brackets across the whole drawing.
    const capW = capIn !== null ? MIN_CAP * 2 : 0;
    const inner = width - (capLeft ? capW : 0) - (capRight ? capW : 0);
    const w = inner / row.pieces.length;
    let x = 0;
    if (capLeft) {
      caps.left = { x, w: capW, label: capText };
      x += capW;
    }
    row.pieces.forEach((piece, i) => {
      pieces.push({ index: piece.index, x, w, label: piece.value?.display ?? null, kind: kindOf(piece.kind), source: piece.source, agreed: row.agreement.values_agreed[i + 1] ?? null });
      x += w;
    });
    if (capRight) caps.right = { x, w: capW, label: capText };
    if (row.printed_overall) printed = { x: 0, w: width, label: row.printed_overall.display };
    if (row.expected_total) needed = { x: 0, w: width, label: row.expected_total.display };
  }

  const delta = formatDelta(row.delta);
  const difference = delta.sign === null ? null : { text: delta.text, sign: delta.sign };
  return {
    status: 'drawn',
    width,
    toScale,
    pieces,
    printed,
    needed,
    caps,
    walls,
    difference,
    held: row.hold,
    summary: summaryOf(row, difference, toScale),
  };
}

/** "Printed 42", needed 44", short by 2"; 3 pieces 13 1/8", 21 3/4", ? ; walls: back wall and both ends." */
export function summaryOf(row: CountertopResult, difference: { text: string; sign: -1 | 0 | 1 } | null, toScale: boolean): string {
  const parts: string[] = [];
  parts.push(row.printed_overall ? `Printed ${row.printed_overall.display}` : 'Printed overall not read');
  if (row.expected_total) parts.push(`needed ${row.expected_total.display}`);
  if (difference) {
    const amount = difference.text.replace(/^[−+]/, '');
    parts.push(difference.sign === 0 ? 'no difference' : difference.sign < 0 ? `short by ${amount}` : `over by ${amount}`);
  }
  const pieceText = row.pieces.map((p) => p.value?.display ?? '?').join(', ');
  const walls = row.wall_layout.label ?? 'walls not established';
  let sentence = `${parts.join(', ')}; ${row.pieces.length} ${row.pieces.length === 1 ? 'piece' : 'pieces'} ${pieceText}; ${walls}.`;
  if (!toScale) sentence += ' Not to scale: some widths are missing.';
  if (row.hold) sentence += ` Held: ${row.hold.reason}`;
  return sentence;
}
