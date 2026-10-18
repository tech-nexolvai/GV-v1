/**
 * Chat v4 (CONTRACT4-full-check, #1483): the one-click full check and the product chips, kept free
 * of React so they can be tested alone.
 *
 * The owner: "A human uploads 2 PDFs; there is one button, full run check… the cloud drawing (our
 * code does it, the MAIN thing)… where they select countertop/cabinet, add a toggle to select the
 * product." So the composer carries Countertop / Cabinet chips (remembered per drawing set), a
 * "Run full check" button sends `mode: "full_check"`, and a full-check answer leads with the cloud
 * drawing and the check report, then What's wrong / To check / What's right.
 *
 * Products are data (#1547): the chips are the product registry's active products (`GET /products`,
 * `@/lib/products`), never a list here; a product added to the registry gets its chip with no change.
 */

import type { ChatFile, ChatFinding, ChatProduct, FindingSource } from '@/lib/chat-api';
import type { ViewMessage } from '@/lib/chat-messages';
import { proofGroup } from '@/lib/chat-proof';
import { activeProducts, inRegistryOrder, productCatalog, productPlural, type ProductCatalog } from '@/lib/products';

/** The products a chip can pick now: the registry's active ones, in its order. */
export function pickable(catalog: ProductCatalog = productCatalog()): ChatProduct[] {
  return activeProducts(catalog).map((product) => product.id);
}

/** Products as a heading says them: "Countertops", "Countertops + Cabinets" (registry words). */
export function productsWords(products: readonly ChatProduct[] | null | undefined, catalog: ProductCatalog = productCatalog()): string | null {
  if (!products?.length) return null;
  return inRegistryOrder(products, catalog).map((product) => productPlural(product, catalog)).join(' + ');
}

/** The text a full check is sent with (the server runs the app's own instruction for `full_check`). */
export const FULL_CHECK_TEXT = 'Full check';

// ── Remembered per drawing set ──────────────────────────────

const PRODUCTS_KEY = 'gv-chat-products:';

/**
 * The products picked for this drawing set before, or null when none were (or storage is blocked).
 * Once the products have loaded, one that cannot be picked any more is left out.
 */
export function readProducts(storage: Pick<Storage, 'getItem'> | null, packageId: string, catalog: ProductCatalog = productCatalog()): ChatProduct[] | null {
  try {
    const raw = storage?.getItem(PRODUCTS_KEY + packageId);
    if (!raw) return null;
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return null;
    const allowed = catalog.length ? pickable(catalog) : null;
    const products = parsed.filter((product): product is string => typeof product === 'string' && (allowed === null || allowed.includes(product)));
    return products.length ? inRegistryOrder(products, catalog) : null;
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
 * product type, else every active product (a set with no product stated is checked for all, as the
 * server does). The pick is a strict scope (Anant, 2026-10-18), so it is always shown. Null only
 * while the products have not loaded and nothing else is known.
 */
export function effectiveProducts(
  picked: readonly ChatProduct[] | null,
  remembered: readonly ChatProduct[] | null,
  productType: ChatProduct | null | undefined,
  catalog: ProductCatalog = productCatalog(),
): ChatProduct[] | null {
  if (picked?.length) return [...picked];
  if (remembered?.length) return [...remembered];
  // The set's own product, when it can be picked.
  if (productType && (!catalog.length || pickable(catalog).includes(productType))) return [productType];
  const every = pickable(catalog);
  return every.length ? every : null;
}

/** True when every active product is picked ("All"). */
export function allPicked(products: readonly ChatProduct[] | null | undefined, catalog: ProductCatalog = productCatalog()): boolean {
  const every = pickable(catalog);
  return every.length > 0 && every.every((product) => products?.includes(product));
}

/**
 * The scope as the result header says it: "Checked: Countertops", "Checked: All products".
 * Null when the products are not known (an older answer).
 */
export function checkedWords(products: readonly ChatProduct[] | null | undefined, catalog: ProductCatalog = productCatalog()): string | null {
  if (!products?.length) return null;
  const words = productsWords(products, catalog);
  return allPicked(products, catalog) && pickable(catalog).length > 1 ? 'Checked: All products' : `Checked: ${words}`;
}

/**
 * The scope in one sentence for the Run popover: "Only countertops are checked; cabinets are left
 * out." or "Every product is checked: countertops and cabinets."
 */
export function scopeSentence(products: readonly ChatProduct[] | null | undefined, catalog: ProductCatalog = productCatalog()): string | null {
  if (!products?.length) return null;
  const lower = (ids: readonly ChatProduct[]) => inRegistryOrder(ids, catalog).map((id) => productPlural(id, catalog).toLowerCase());
  const listed = (words: string[]) => (words.length < 2 ? words.join('') : `${words.slice(0, -1).join(', ')} and ${words[words.length - 1]}`);
  const left = pickable(catalog).filter((product) => !products.includes(product));
  if (!left.length) return `Every product is checked: ${listed(lower(products))}.`;
  // Product names are plural ("countertops"), so "are" either way.
  return `Only ${listed(lower(products))} are checked; ${listed(lower(left))} are left out.`;
}

/** A chip pressed: on or off, but never the last one off (at least one product stays picked). */
export function toggleProduct(current: readonly ChatProduct[] | null, product: ChatProduct, catalog: ProductCatalog = productCatalog()): ChatProduct[] {
  const now = current ?? [];
  if (!now.includes(product)) return inRegistryOrder([...now, product], catalog);
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
  // "Continue the full check · …" is a full check that goes on after a server restart (#1514).
  return question?.mode == null && /^(?:Full|Continue the full) check(?: · [^\n]*)?$/.test(question?.text?.trim() ?? '');
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

/** #1619: "… - Not compared.pdf" and "… - Not compared.xlsx" (".csv" without openpyxl). */
const NOT_COMPARED = / - Not compared\.(pdf|xlsx|csv)$/i;

/**
 * The cloud drawing (the main output), the check report, and any other files, from a message's
 * files: by the server's `kind`, else by name ("cloud", "report") for a file sent before kinds.
 */
export function fullCheckFiles(files: readonly ChatFile[]): { cloud: ChatFile | null; report: ChatFile | null; notCompared: ChatFile[]; rest: ChatFile[] } {
  const pdf = (file: ChatFile) => file.mime === 'application/pdf' || /\.pdf$/i.test(file.name);
  // A file sent before kinds: "cloud" in its name, else the full check's "… - AI markup.pdf".
  // The NEWEST of each (#1525: "Update cloud drawing" adds a new drawing and report; older ones are kept).
  const last = (test: (file: ChatFile) => boolean) => [...files].reverse().find(test) ?? null;
  const clouds = files.filter((file) => file.kind === 'cloud_drawing' || (!file.kind && pdf(file) && (/cloud/i.test(file.name) || /\bAI markup\b/i.test(file.name))));
  const reports = files.filter((file) => !clouds.includes(file) && (file.kind === 'check_report' || (!file.kind && pdf(file) && /report/i.test(file.name))));
  const cloud = last((file) => file.kind === 'cloud_drawing' && clouds.includes(file)) ?? last((file) => clouds.includes(file));
  const report = last((file) => reports.includes(file));
  // #1619: the Not-compared list, as a PDF and a workbook (the newest of each).
  const lists = files.filter((file) => NOT_COMPARED.test(file.name));
  const notCompared = [
    last((file) => lists.includes(file) && /\.pdf$/i.test(file.name)),
    last((file) => lists.includes(file) && /\.(xlsx|csv)$/i.test(file.name)),
  ].filter((file): file is ChatFile => file !== null);
  // Older drawings and reports are kept on the server but not shown again as plain file cards.
  return { cloud, report, notCompared, rest: files.filter((file) => !clouds.includes(file) && !reports.includes(file) && !lists.includes(file)) };
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

/**
 * The page the cloud drawing's preview shows: the vendor page of the biggest clouded item (#1581:
 * only code-proved Wrong items are clouded), else the first wrong item's, else the first item's,
 * else 1. It is the vendor file's own page number (the drawing labels its pages so).
 */
export function previewPage(findings: readonly ChatFinding[]): number {
  const clouded = findings
    .filter((finding) => proofGroup(finding) === 'clouded')
    .sort((a, b) => (b.impact?.score ?? 0) - (a.impact?.score ?? 0));
  const first = clouded[0] ?? findings.find((finding) => finding.status === 'different') ?? findings.find((finding) => finding.status === 'check') ?? findings[0];
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
