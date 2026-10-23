/**
 * The result screen's drawer (results v2, 2026-10-23, built on top of #1717's evidence panel): two
 * big pictures side by side ("Vendor shows" with its spot ringed; "Should be" with the architect's
 * spot ringed green, or the vendor's own measured spot, or what drawing is needed), a "How we know"
 * box of two or three plain numbered steps, and small chips. Kept free of React so it is tested on
 * its own. No internal names reach the reviewer.
 */

import type { ChatFinding, FindingBox, FindingDecision, ProofGroup } from '@/lib/chat-api';
import { ruleNoteWords } from '@/lib/chat-proof';
import { isDrawingNeededItem, isRefusedString, shouldBeValue, vendorValue, wrongPartWords } from '@/lib/chat-result';
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

/**
 * Where a smaller spot inside the crop's own spot is on that crop (a refused string's printed
 * overall inside its row): the same shape check as `ringOnCrop`, then the inner box placed in it.
 */
export function ringInCrop(crop: FindingBox | null | undefined, inner: { x0: number; y0: number; x1: number; y1: number } | null | undefined, naturalWidth: number, naturalHeight: number): Ring | null {
  if (!crop || !inner || !ringOnCrop(crop, naturalWidth, naturalHeight)) return null;
  const left = Math.min(crop.x0, crop.x1) - CROP_PAD_PT;
  const top = Math.min(crop.y0, crop.y1) - CROP_PAD_PT;
  const paddedWidth = Math.abs(crop.x1 - crop.x0) + 2 * CROP_PAD_PT;
  const paddedHeight = Math.abs(crop.y1 - crop.y0) + 2 * CROP_PAD_PT;
  const x0 = Math.min(inner.x0, inner.x1);
  const y0 = Math.min(inner.y0, inner.y1);
  const width = Math.abs(inner.x1 - inner.x0);
  const height = Math.abs(inner.y1 - inner.y0);
  if (width <= 0 || height <= 0) return null;
  const ring = { left: (x0 - left) / paddedWidth, top: (y0 - top) / paddedHeight, width: width / paddedWidth, height: height / paddedHeight };
  // Only a spot that is really inside the crop is ringed.
  if (ring.left < 0 || ring.top < 0 || ring.left + ring.width > 1.001 || ring.top + ring.height > 1.001) return null;
  return ring;
}

/**
 * The printed overall's own spot inside a vendor string's row, when code kept the label it read
 * (`dims.vendor`, on the row's page); else none (the row alone is ringed).
 */
export function overallSpot(finding: Pick<ChatFinding, 'dims' | 'vendor_box'>): FindingBox | null {
  const label = finding.dims?.vendor;
  const row = finding.vendor_box;
  if (!label || !row || label.page !== row.page || !Array.isArray(label.box) || label.box.length !== 4) return null;
  if ((label.file ?? null) !== (row.file ?? null) && label.file && row.file) return null;
  const [x0, y0, x1, y1] = label.box;
  return { page: label.page, x0, y0, x1, y1, file: label.file ?? null };
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
  } else if (kind === 'not_compared' && isRefusedString(finding, 'not_compared')) {
    steps.push(...refusedStringSteps(finding));
  } else if (kind === 'not_compared' && architectSaysSomething(finding)) {
    steps.push(...architectWordsSteps(finding));
  } else {
    steps.push(sentence(finding.proof?.reason ?? finding.why));
    steps.push(finding.proof?.settle ? sentence(`Needed to compare it: ${finding.proof.settle}`) : null);
  }
  if (options.noArchitectDrawing && kind !== 'drawing_needed') steps.push('This page has no architect drawing in the upload: it is in the drawings request to GV.');
  const unique = [...new Set(steps.filter((step): step is string => Boolean(step)))];
  return unique.slice(0, 3);
}

/** The sentence a refused vendor string opens with: nothing about its numbers. */
export const REFUSED_STRING_WORDS = 'The app could not confirm these sizes belong to one row, so nothing is compared.';

/**
 * How we know, for a vendor string code did not compare: that nothing is compared, code's own
 * reason (without the AI's unproven numbers), and that nothing goes to the vendor for it.
 */
export function refusedStringSteps(finding: Pick<ChatFinding, 'proof' | 'why'>): string[] {
  const steps: (string | null)[] = [];
  const unverified = !finding.proof?.not_compared || finding.proof.not_compared === 'string_not_verified';
  steps.push(unverified ? REFUSED_STRING_WORDS : 'The app did not compare this row’s sizes, so nothing is compared.');
  // Code's reason, without a leading "parts 3″ + … = …; printed …;" (the unproven reading itself).
  const reason = (finding.proof?.reason ?? finding.why ?? '').replace(/^parts\s[^;]*;\s*printed[^;]*;\s*/i, '').trim();
  const restates = /could not verify on the drawing that these parts are one string/i.test(reason);
  if (reason && !restates) steps.push(sentence(reason.replace(/^code(?!\u2019s|'s)\b/i, 'The app')));
  steps.push('Nothing is asked of the vendor for it. If the row looks wrong to you, you can add a red mark yourself.');
  return steps.filter((step): step is string => Boolean(step));
}

/** Code's catch-all words for "the architect prints nothing here" (`classification`, R7). */
const GIVES_NOTHING = /^architect gives no size or information\b/i;

/**
 * A Not-compared item whose architect side holds words (a tag, a note, "no overall printed (partial
 * dims …)") while code's reason says the architect "gives no size or information": the screen shows
 * the architect's words, so "How we know" must not say the architect gives nothing (the client
 * meeting, 2026-10-23). Steps are said from the item's own reasons instead.
 */
export function architectSaysSomething(finding: Pick<ChatFinding, 'architect' | 'proof'>): boolean {
  const words = finding.architect?.trim() ?? '';
  if (!words || words === '—') return false;
  return GIVES_NOTHING.test(finding.proof?.reason ?? '');
}

/** How we know, for an item whose architect side holds words (or a size) code could not compare. */
export function architectWordsSteps(finding: Pick<ChatFinding, 'architect' | 'vendor' | 'proof' | 'check_reason' | 'keep_reason'>): string[] {
  const words = marks((finding.architect ?? '').trim()).replace(/[.;,]+$/, '');
  if (/^(?:no\b.*\bdrawing\b|not paired\b)/i.test(words)) {
    return ['No architect drawing was paired with this item, so it was not compared.'];
  }
  const size = /\d/.test(words) && !/[a-z]{2,}/i.test(words);
  const steps: (string | null)[] = [];
  steps.push(size ? `The architect’s drawing prints ${words}.`
    : /^(?:no|none|not)\b/i.test(words) ? `On the architect’s drawing: ${words}.`
      : `The architect’s drawing shows “${words}”, but no size for it.`);
  // Step 2 is the item's own reason: what the architect's words say comes first; "the same piece"
  // only when the architect does give something and code could not tie it to the vendor's item.
  const vendor = vendorValue(finding).main;
  const theirs = vendor && vendor !== '—' ? `the vendor’s ${vendor}` : 'the vendor’s value';
  if (/\bno overall\b/i.test(words) && /\bpartial\b/i.test(words)) steps.push(`The architect prints only partial sizes, so there is no overall to compare with ${theirs}.`);
  else if (/^(?:no|none|not)\b/i.test(words)) steps.push(`The architect prints no size for it, so there is no size to measure ${theirs} against.`);
  else if (finding.check_reason === 'pairing_not_certain') steps.push('The app could not confirm it is the same piece as the vendor’s, so the two were not compared.');
  else if (size) steps.push('The app could not use it as this item’s size, so nothing is compared.');
  else steps.push(`There is no architect size to measure ${theirs} against, so nothing is compared.`);
  steps.push(finding.proof?.settle ? sentence(`Needed to compare it: ${finding.proof.settle}`) : null);
  return steps.filter((step): step is string => Boolean(step));
}

/** Why the architect's side has no picture, in plain words. */
export function noArchitectPictureWords(finding: Pick<ChatFinding, 'proof' | 'source' | 'rule_check_id' | 'architect'>, noDrawing: boolean): string {
  if (noDrawing) return 'No architect drawing of this item in the upload';
  if (finding.source === 'rules' || finding.rule_check_id) return 'GV’s rule decides this item: there is no architect drawing to show';
  // Words on the architect's side (shown above) mean the architect does say something: only its spot is unknown.
  if (finding.proof?.not_compared === 'nothing_to_compare' && !architectSaysSomething(finding)) return 'The architect’s drawings give no size or note for this item, so there is no spot to show';
  return 'The app did not find this item’s spot on the architect’s drawings, so there is no picture of it';
}

// ── One item, one card ─────────────────────────────────────

const COPY_GROUP: Record<string, string> = {
  clouded: 'Wrong',
  likely_problem: 'Likely problem',
  rule_note: 'Ask the architect',
  not_compared: 'Not compared',
  right: 'Right',
  dismissed: 'Right (reviewer)',
};

/**
 * The other copies of this item code folded into its card (2026-10-23), one line each: who
 * reported it, its card, its values, and why it could not be proved; first, when they disagree on
 * the architect's size, why the card is Not compared.
 */
export function copyLines(finding: Pick<ChatFinding, 'same_item'>): string[] {
  const folded = finding.same_item;
  if (!folded) return [];
  const lines: string[] = [];
  if (folded.conflict?.length) lines.push(`The copies pair the vendor’s number with different architect sizes (${folded.conflict.map(marks).join(', ')}), so it is not compared.`);
  for (const copy of folded.copies ?? []) {
    const who = copy.by ? `${copy.by.charAt(0).toUpperCase()}${copy.by.slice(1)}` : 'Another reader';
    const group = copy.group ? COPY_GROUP[copy.group] ?? 'Listed' : 'Listed';
    const values = [copy.vendor ? `vendor ${marks(copy.vendor)}` : null, copy.architect ? `architect ${marks(copy.architect)}` : null].filter(Boolean).join(', ');
    const why = copy.reason?.trim();
    lines.push(`${who}: ${group}${values ? ` (${values})` : ''}${why ? `: ${why.replace(/\.$/, '')}` : ''}.`);
  }
  return lines;
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
