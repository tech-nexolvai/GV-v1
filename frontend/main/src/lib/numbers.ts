/**
 * Numbers inside plain words, so they can be set in the number face.
 *
 * Moved here unchanged on 2026-10-28 from `lib/review-items.ts` (the old Review list), because the
 * chat's `NumText` uses it.
 */

/** Splits text so numbers (with fractions and inch marks) can be set in the number face. */
export function splitNumbers(text: string): { text: string; number: boolean }[] {
  const parts: { text: string; number: boolean }[] = [];
  const pattern = /\d+(?:\s\d+\/\d+|\/\d+|\.\d+)?(?:\s?(?:"|in\b))?/g;
  let last = 0;
  for (const match of text.matchAll(pattern)) {
    const at = match.index ?? 0;
    if (at > last) parts.push({ text: text.slice(last, at), number: false });
    parts.push({ text: match[0], number: true });
    last = at + match[0].length;
  }
  if (last < text.length) parts.push({ text: text.slice(last), number: false });
  return parts;
}
