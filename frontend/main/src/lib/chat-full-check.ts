/**
 * Chat v4 (CONTRACT4-full-check, #1483): the one-click full check and the product chips, kept free
 * of React so they can be tested alone.
 *
 * The owner: "A human uploads 2 PDFs; there is one button, full run check… the cloud drawing (our
 * code does it, the MAIN thing)… where they select countertop/cabinet, add a toggle to select the
 * product." So the composer carries Countertop / Cabinet chips (remembered per drawing set), a
 * "Run full check" button sends `mode: "full_check"`, and a full-check answer leads with the cloud
 * drawing and the check report, then What's wrong / To check / What's right.
 */

import type { ChatFile, ChatFinding, ChatProduct, FindingSource } from '@/lib/chat-api';
import type { ViewMessage } from '@/lib/chat-messages';

export const PRODUCTS: readonly ChatProduct[] = ['countertop', 'cabinet'];

/** A chip's word. */
export const PRODUCT_WORDS: Record<ChatProduct, string> = { countertop: 'Countertop', cabinet: 'Cabinet' };

/** Products as a heading says them: "Countertops", "Countertops + Cabinets". */
export function productsWords(products: readonly ChatProduct[] | null | undefined): string | null {
  const known = PRODUCTS.filter((product) => products?.includes(product));
  if (!known.length) return null;
  return known.map((product) => `${PRODUCT_WORDS[product]}s`).join(' + ');
}

/** The text a full check is sent with (the server runs the app's own instruction for `full_check`). */
export const FULL_CHECK_TEXT = 'Full check';

// ── Remembered per drawing set ──────────────────────────────

const PRODUCTS_KEY = 'gv-chat-products:';

/** The products picked for this drawing set before, or null when none were (or storage is blocked). */
export function readProducts(storage: Pick<Storage, 'getItem'> | null, packageId: string): ChatProduct[] | null {
  try {
    const raw = storage?.getItem(PRODUCTS_KEY + packageId);
    if (!raw) return null;
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return null;
    const products = PRODUCTS.filter((product) => parsed.includes(product));
    return products.length ? products : null;
  } catch {
    return null;
  }
}

export function saveProducts(storage: Pick<Storage, 'setItem'> | null, packageId: string, products: readonly ChatProduct[]): void {
  try {
    storage?.setItem(PRODUCTS_KEY + packageId, JSON.stringify(products));
  } catch {
    // Blocked storage (a private window): the choice simply lasts for this visit.
  }
}

/**
 * The chips' products: the reviewer's pick, else what this set remembered, else the set's own
 * product type. Null when none of these is known (the server then uses the set's product type).
 */
export function effectiveProducts(
  picked: readonly ChatProduct[] | null,
  remembered: readonly ChatProduct[] | null,
  productType: ChatProduct | null | undefined,
): ChatProduct[] | null {
  if (picked?.length) return [...picked];
  if (remembered?.length) return [...remembered];
  return productType && PRODUCTS.includes(productType) ? [productType] : null;
}

/** A chip pressed: on or off, but never the last one off (at least one product stays picked). */
export function toggleProduct(current: readonly ChatProduct[] | null, product: ChatProduct): ChatProduct[] {
  const now = current ?? [];
  if (!now.includes(product)) return PRODUCTS.filter((each) => each === product || now.includes(each));
  const next = now.filter((each) => each !== product);
  return next.length ? next : [...now];
}

// ── Words ───────────────────────────────────────────────────

/** The user bubble of a full check: "Full check · Countertops · High effort". */
export function fullCheckBubble(products: readonly ChatProduct[] | null | undefined, effortWords: string | null): string {
  return ['Full check', productsWords(products), effortWords ? `${effortWords} effort` : null].filter(Boolean).join(' · ');
}

/** The confirm line: "Full check · Countertops · Opus 5.5 · High · up to $15". */
export function confirmWords(products: readonly ChatProduct[] | null, model: string | null, effortWords: string | null, cap: string | null): string {
  return ['Full check', productsWords(products) ?? 'this set’s product', model, effortWords, cap ? `up to ${cap}` : null].filter(Boolean).join(' · ');
}

/**
 * True for an answer to a full check: its own mode, or the question it answers. A saved chat's
 * messages come back without their mode, so a question saved in the full check's own words
 * ("Full check", "Full check · Countertops · High effort") counts too (#1508).
 */
export function isFullCheck(message: Pick<ViewMessage, 'mode'>, question?: Pick<ViewMessage, 'mode'> & { text?: string | null } | null): boolean {
  if (message.mode === 'full_check' || question?.mode === 'full_check') return true;
  return question?.mode == null && /^Full check(?: · [^\n]*)?$/.test(question?.text?.trim() ?? '');
}

/** What a right finding matched, under its value: "matches architect". */
export function matchesWords(source: FindingSource): string {
  switch (source) {
    case 'brand_prototype':
      return 'matches brand prototype';
    case 'rules':
      return 'meets GV rule';
    case 'vendor_arithmetic':
      return 'adds up';
    case 'cross_view':
      return 'same on every view';
    default:
      return 'matches architect';
  }
}

// ── The files a full check made ─────────────────────────────

/**
 * The cloud drawing (the main output), the check report, and any other files, from a message's
 * files: by the server's `kind`, else by name ("cloud", "report") for a file sent before kinds.
 */
export function fullCheckFiles(files: readonly ChatFile[]): { cloud: ChatFile | null; report: ChatFile | null; rest: ChatFile[] } {
  const pdf = (file: ChatFile) => file.mime === 'application/pdf' || /\.pdf$/i.test(file.name);
  // A file sent before kinds: "cloud" in its name, else the full check's "… - AI markup.pdf".
  // The NEWEST of each (#1525: "Update cloud drawing" adds a new drawing and report; older ones are kept).
  const last = (test: (file: ChatFile) => boolean) => [...files].reverse().find(test) ?? null;
  const clouds = files.filter((file) => file.kind === 'cloud_drawing' || (!file.kind && pdf(file) && (/cloud/i.test(file.name) || /\bAI markup\b/i.test(file.name))));
  const reports = files.filter((file) => !clouds.includes(file) && (file.kind === 'check_report' || (!file.kind && pdf(file) && /report/i.test(file.name))));
  const cloud = last((file) => file.kind === 'cloud_drawing' && clouds.includes(file)) ?? last((file) => clouds.includes(file));
  const report = last((file) => reports.includes(file));
  // Older drawings and reports are kept on the server but not shown again as plain file cards.
  return { cloud, report, rest: files.filter((file) => !clouds.includes(file) && !reports.includes(file)) };
}

// ── Live progress while a full check runs (PATH step 3, #1522) ─

/**
 * One calm line while a full check runs, from what has streamed so far: "Checking page 12 of 60 ·
 * 8 wrong so far · about 6 min left". The page count comes from the coverage map (pages the AI has
 * accounted for); the time left is the time per page so far times the pages left, shown only once
 * three pages are done and the total is known. Without coverage yet: "Starting the full check…".
 */
export function progressWords(
  message: Pick<ViewMessage, 'created_at' | 'findings' | 'coverage' | 'progress'>,
  now: number = Date.now(),
): string {
  // #1524: the server's own progress when it sends it.
  const live = message.progress;
  if (live) {
    if (live.phase === 'reading' && !live.pages_done) return 'Reading the drawings…';
    if (live.phase === 'writing') return `Writing the summary and the cloud drawing · ${live.wrong_so_far} wrong · ${live.decision_so_far} to decide`;
    const at = live.pages_total ? `Checking page ${Math.min(live.pages_done + 1, live.pages_total)} of ${live.pages_total}` : `Checking · ${live.pages_done} pages done`;
    const left = live.eta_seconds == null ? null : live.eta_seconds < 60 ? 'under a minute left' : `about ${Math.ceil(live.eta_seconds / 60)} min left`;
    return [at, `${live.wrong_so_far} wrong so far`, left].filter(Boolean).join(' · ');
  }
  const pages = message.coverage?.pages ?? [];
  const done = new Set(pages.filter((page) => page.status !== 'not_opened').map((page) => page.page)).size;
  const total = message.coverage?.counts?.total ?? null;
  const wrong = (message.findings ?? []).filter((finding) => finding.status === 'different').length;
  if (!done && !wrong) return 'Starting the full check…';
  const parts = [total ? `Checking page ${Math.min(done + 1, total)} of ${total}` : `Checking · ${done} ${done === 1 ? 'page' : 'pages'} done`];
  parts.push(`${wrong} wrong so far`);
  const started = Date.parse(message.created_at);
  if (total && done >= 3 && done < total && Number.isFinite(started) && now > started) {
    const minutes = (((now - started) / done) * (total - done)) / 60000;
    parts.push(minutes < 1 ? 'under a minute left' : `about ${Math.ceil(minutes)} min left`);
  }
  return parts.join(' · ');
}

/** The page the cloud drawing's preview shows: the first wrong item's vendor page, else the first item's, else 1. */
export function previewPage(findings: readonly Pick<ChatFinding, 'status' | 'page' | 'vendor_box'>[]): number {
  const first = findings.find((finding) => finding.status === 'different') ?? findings.find((finding) => finding.status === 'check') ?? findings[0];
  return first ? first.vendor_box?.page ?? first.page : 1;
}

/**
 * A summary GV Review wrote itself (`by: "code"`, #1524) ends with an italic line saying so. The
 * screen shows that once, as a quiet note under the summary, so the line is taken off the words.
 */
export function withoutCodeNote(text: string): { text: string; note: string | null } {
  const lines = text.trimEnd().split('\n');
  const last = lines[lines.length - 1]?.trim() ?? '';
  const match = /^[*_](Summary written by GV Review[^*_]*)[*_]$/.exec(last);
  if (!match) return { text, note: null };
  return { text: lines.slice(0, -1).join('\n').trimEnd(), note: match[1].trim() };
}
