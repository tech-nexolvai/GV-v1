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

import type { ChatFile, ChatProduct, FindingSource } from '@/lib/chat-api';
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

/**
 * The cloud drawing (the main output), the check report, and any other files, from a message's
 * files: by the server's `kind`, else by name ("cloud", "report") for a file sent before kinds.
 */
export function fullCheckFiles(files: readonly ChatFile[]): { cloud: ChatFile | null; report: ChatFile | null; rest: ChatFile[] } {
  const pdf = (file: ChatFile) => file.mime === 'application/pdf' || /\.pdf$/i.test(file.name);
  // A file sent before kinds: "cloud" in its name, else the full check's "… - AI markup.pdf".
  const cloud = files.find((file) => file.kind === 'cloud_drawing')
    ?? files.find((file) => !file.kind && pdf(file) && /cloud/i.test(file.name))
    ?? files.find((file) => !file.kind && pdf(file) && /\bAI markup\b/i.test(file.name)) ?? null;
  const report = files.find((file) => file.kind === 'check_report' && file !== cloud)
    ?? files.find((file) => !file.kind && file !== cloud && pdf(file) && /report/i.test(file.name)) ?? null;
  return { cloud, report, rest: files.filter((file) => file !== cloud && file !== report) };
}
