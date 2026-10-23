/**
 * The result screen's drawer (results v2, 2026-10-23, built on top of #1717's evidence panel): two
 * big pictures side by side ("Vendor shows" with its spot ringed; "Should be" with the architect's
 * spot ringed green, or the vendor's own measured spot, or what drawing is needed), a "How we know"
 * box of two or three plain numbered steps, and small chips. Kept free of React so it is tested on
 * its own. No internal names reach the reviewer.
 */

import type { ChatFinding, FindingBox, FindingDecision, ProofGroup } from '@/lib/chat-api';
import { ruleNoteWords } from '@/lib/chat-proof';
import { isDrawingNeededItem, shouldBeValue, vendorValue, wrongPartWords } from '@/lib/chat-result';
import { inchNumber, inchWords } from '@/lib/chat-trust';

/** The drawer's group, with Drawing needed apart (a page, not a finding). */
export type DrawerKind = ProofGroup | 'drawing_needed';

export function drawerKind(finding: ChatFinding, group: ProofGroup | null): DrawerKind {
  if (isDrawingNeededItem(finding)) return 'drawing_needed';
  return group ?? 'not_compared';
}

/** The header's words beside the glyph ("Wrong · proven"): the word, then how sure (left out on a phone). */
export const KIND_WORDS: Record<DrawerKind, { word: string; sure: string | null }> = {
  clouded: { word: 'Wrong', sure: 'proven' },
  likely_problem: { word: 'Likely problem', sure: 'not proven' },
  rule_note: { word: 'Ask the architect', sure: null },
  drawing_needed: { word: 'Drawing needed', sure: null },
  right: { word: 'Right', sure: null },
  dismissed: { word: 'Right (reviewer)', sure: null },
  not_compared: { word: 'Not compared', sure: null },
};

// ── The rings ───────────────────────────────────────────────

/** A ring on a picture, as fractions of the picture's own width and height. */
export interface Ring {
  left: number;
  top: number;
  width: number;
  height: number;
}

/** Code pads each evidence crop by this many PDF points round its spot (`chat_drawing.CROP_PAD_PT`). */
export const CROP_PAD_PT = 24;

/**
 * Where the spot is on its evidence crop. The crop is the spot padded on each side; near a page edge
 * the padding is cut, and then the ring could only be guessed: no ring when the picture's shape does
 * not match the padded spot's (within 3 %).
 */
export function ringOnCrop(box: FindingBox | null | undefined, naturalWidth: number, naturalHeight: number): Ring | null {
  if (!box || naturalWidth <= 0 || naturalHeight <= 0) return null;
  const width = Math.abs(box.x1 - box.x0);
  const height = Math.abs(box.y1 - box.y0);
  if (width <= 0 || height <= 0) return null;
  const paddedWidth = width + 2 * CROP_PAD_PT;
  const paddedHeight = height + 2 * CROP_PAD_PT;
  const expected = paddedWidth / paddedHeight;
  const actual = naturalWidth / naturalHeight;
  if (Math.abs(actual - expected) / expected > 0.03) return null;
  return { left: CROP_PAD_PT / paddedWidth, top: CROP_PAD_PT / paddedHeight, width: width / paddedWidth, height: height / paddedHeight };
}

/** Where the spot is on the vendor's whole-page picture (100 dpi, drawn from PDF points). */
export function ringOnPage(box: FindingBox | null | undefined, naturalWidth: number, naturalHeight: number, dpi = 100): Ring | null {
  if (!box || naturalWidth <= 0 || naturalHeight <= 0) return null;
  const px = (points: number) => (points * dpi) / 72;
  const left = px(Math.min(box.x0, box.x1)) / naturalWidth;
  const top = px(Math.min(box.y0, box.y1)) / naturalHeight;
  const width = px(Math.abs(box.x1 - box.x0)) / naturalWidth;
  const height = px(Math.abs(box.y1 - box.y0)) / naturalHeight;
  if (left >= 1 || top >= 1 || width <= 0 || height <= 0) return null;
  return { left, top, width, height };
}

// ── The two sides ──────────────────────────────────────────

/** The vendor's own drawing is the reference: its parts added up, or the same span measured on another view. */
export function vendorOnly(finding: Pick<ChatFinding, 'source' | 'created_by' | 'arch_picture_id' | 'arch_box' | 'parts'>): boolean {
  if (finding.arch_picture_id) return false;
  return finding.source === 'vendor_arithmetic' || finding.source === 'cross_view' || finding.created_by === 'consistency' || Boolean(finding.parts?.length);
}

/** The architect's spot is on the vendor's own page picture (one sheet holds both drawings). */
export function architectOnVendorPage(finding: Pick<ChatFinding, 'vendor_box' | 'arch_box'>): boolean {
  const vendor = finding.vendor_box;
  const architect = finding.arch_box;
  if (!architect) return false;
  if (!vendor) return false;
  return architect.page === vendor.page && (architect.file ?? null) === (vendor.file ?? null);
}

export interface SideWords {
  label: string;
  value: string;
  tone: 'wrong' | 'likely' | 'right' | null;
}

/** "Vendor shows 14″" (red when proven wrong). */
export function vendorSide(finding: ChatFinding, kind: DrawerKind): SideWords {
  if (kind === 'drawing_needed') return { label: 'Vendor page', value: `p.${finding.page}`, tone: null };
  const value = vendorValue(finding).main || '—';
  return { label: 'Vendor shows', value, tone: kind === 'clouded' ? 'wrong' : kind === 'likely_problem' ? 'likely' : kind === 'right' || kind === 'dismissed' ? 'right' : null };
}

/** "Should be 11 5/8″" (green), or what the architect shows, or the GV rule. */
export function referenceSide(finding: ChatFinding, kind: DrawerKind, decision: FindingDecision | null | undefined = finding.decision): SideWords {
  if (kind === 'drawing_needed') return { label: 'Architect’s drawing', value: 'Not in the upload', tone: null };
  const part = kind === 'clouded' && decision?.decision !== 'agree' ? wrongPartWords(finding) : null;
  if (part) return { label: 'Should be', value: part.should, tone: 'right' };
  const should = kind === 'clouded' ? shouldBeValue(finding, decision) : null;
  if (should) return { label: 'Should be', value: should.main, tone: 'right' };
  const architect = finding.architect?.trim() ? finding.architect.trim().replace(/"/g, '″').replace(/'/g, '′') : '—';
  if (kind === 'rule_note') return { label: 'Architect shows', value: architect, tone: null };
  if (finding.source === 'rules' || finding.rule_check_id) return { label: 'GV’s rule', value: architect, tone: kind === 'right' ? 'right' : null };
  return { label: vendorOnly(finding) ? 'Vendor’s own drawing' : 'Architect shows', value: architect, tone: kind === 'right' || kind === 'dismissed' ? 'right' : null };
}

// ── How we know ────────────────────────────────────────────

/** Words that are the system's own working names: a step holding one is left out. */
const INTERNAL = /\b(?:helpers?|lead|critic|advocate|matcher|escalation|gap closers?|provers?|roster|ledger|AI|R\d{1,2}|blind pick|look-again)\b/i;

const marks = (text: string) => text.replace(/"/g, '″').replace(/(\d)'/g, '$1′');

function sentence(text: string | null | undefined): string | null {
  const words = (text ?? '').replace(/\s+/g, ' ').trim();
  if (!words || INTERNAL.test(words)) return null;
  const upper = words.charAt(0).toUpperCase() + words.slice(1);
  return marks(/[.!?]$/.test(upper) ? upper : `${upper}.`);
}

/**
 * Two or three plain numbered steps saying how the outcome is known, from the finding's own proof
 * and checks (never an internal name). Drawing needed: why, what still ran, what to do.
 */
export function howWeKnow(finding: ChatFinding, kind: DrawerKind, options: { noArchitectDrawing?: boolean } = {}): string[] {
  const steps: (string | null)[] = [];
  const architect = inchNumber(finding.inches?.architect);
  const vendor = inchNumber(finding.inches?.vendor);
  if (kind === 'drawing_needed') {
    steps.push(sentence(finding.why ? `No architect drawing of this page is in the upload: ${finding.why}` : 'No architect drawing of this page is in the upload'));
    steps.push('The vendor’s own drawing was still checked: its strings that must add up and its sizes printed twice.');
    steps.push('Ask GV for the architect’s drawing, then run the check again to compare it.');
  } else if (kind === 'likely_problem') {
    const likely = finding.proof?.likely_problem;
    steps.push(sentence(likely?.words?.replace(/^please check and confirm:\s*/i, '')));
    steps.push(sentence(likely?.reason ?? finding.proof?.reason));
    steps.push('Code could not prove it on the drawings, so it is not a red mark: the vendor is asked to check and confirm.');
  } else if (kind === 'rule_note') {
    const note = ruleNoteWords(finding);
    steps.push(sentence(note?.why ?? finding.proof?.reason));
    steps.push(note ? sentence(`${note.standard}; the vendor prints the architect’s size`) : null);
    steps.push('GV asks the architect to confirm; nothing is asked of the vendor.');
  } else if (kind === 'clouded' && wrongPartWords(finding)) {
    // #1887: code's geometry named the wrong part; the overall is right.
    const part = wrongPartWords(finding)!;
    const sum = finding.parts_sum ? inchNumber(finding.parts_sum) : null;
    const total = sum !== null ? `${inchWords(sum)}″` : /=\s*([^≠]+?)\s*≠/.exec(finding.sum_text ?? '')?.[1]?.trim();
    steps.push(total && part.overall ? `The parts add up to ${marks(total)}, but the overall says ${part.overall}.` : sentence(finding.sum_text));
    if (part.overall) steps.push(`The ${part.overall} line is drawn ${part.overall} long, so the overall is right.`);
    steps.push(part.drawn
      ? `The ${part.part} line is drawn only ${part.drawn} long, so the ${part.part} is the mistake.`
      : `At the drawing’s scale the ${part.part} is the mistake: the overall less the other parts is ${part.should}.`);
  } else if (kind === 'clouded') {
    const parts = finding.parts?.length ? finding.parts.map(marks).join(' + ') : null;
    const sum = finding.parts_sum ? inchNumber(finding.parts_sum) : null;
    if (parts && sum !== null && finding.printed_total) steps.push(`${parts} = ${inchWords(sum)}″, but the overall says ${marks(finding.printed_total)}.`);
    else if (finding.sum_text) steps.push(sentence(finding.sum_text));
    else if (architect !== null && vendor !== null && !vendorOnly(finding)) {
      steps.push(`The vendor prints ${inchWords(vendor)}″; the ${finding.source === 'brand_prototype' ? 'brand prototype' : finding.source === 'rules' ? 'GV rule' : 'architect’s drawing'}${finding.vs_page && finding.source !== 'rules' ? ` (page ${finding.vs_page})` : ''} gives ${inchWords(architect)}″.`);
    }
    steps.push(sentence(finding.proof?.proof));
    const should = shouldBeValue(finding);
    if (should?.note && !INTERNAL.test(should.note)) steps.push(sentence(`So it should be ${should.main} (${should.note})`));
  } else if (kind === 'right' || kind === 'dismissed') {
    if (kind === 'dismissed') steps.push('You marked this Right.');
    const shown = vendorValue(finding).main;
    const reference = finding.architect?.trim();
    if (shown && reference) steps.push(sentence(`The vendor’s ${shown} matches the ${finding.source === 'rules' ? 'GV rule' : 'architect’s'} ${reference}`));
    steps.push(sentence(finding.proof?.reason));
  } else {
    steps.push(sentence(finding.proof?.reason ?? finding.why));
    steps.push(finding.proof?.settle ? sentence(`Needed to compare it: ${finding.proof.settle}`) : null);
  }
  if (options.noArchitectDrawing && kind !== 'drawing_needed') steps.push('This page has no architect drawing in the upload: it is in the drawings request to GV.');
  const unique = [...new Set(steps.filter((step): step is string => Boolean(step)))];
  return unique.slice(0, 3);
}

// ── Chips ──────────────────────────────────────────────────

/** "Also on page 44", "In the comment letter as C-01", "Architect drawing: not in the upload · in the GV request". */
export function drawerChips(finding: ChatFinding, options: { number?: string | null; noArchitectDrawing?: boolean; inRequest?: boolean } = {}): string[] {
  const chips: string[] = [];
  const others = [...new Set((finding.also_on ?? []).map((other) => other.page).filter((page): page is number => typeof page === 'number' && page !== finding.page))].sort((a, b) => a - b);
  if (others.length) chips.push(`Also on ${others.length === 1 ? 'page' : 'pages'} ${others.join(', ')}`);
  if (options.number) chips.push(`In the comment letter as ${options.number}`);
  if (options.noArchitectDrawing) chips.push(`Architect drawing: not in the upload · ${options.inRequest === false ? 'left out of the GV request' : 'in the GV request'}`);
  return chips;
}
