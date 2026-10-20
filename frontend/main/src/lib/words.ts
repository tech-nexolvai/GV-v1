/**
 * The reviewer's words, one meaning each (#1712, UI redesign step 1; design spec section 7).
 *
 * Every chat, shell and tracker screen takes its outcome and voice words from here, and
 * `tests/vitest/reviewer-words.test.ts` fails when a retired word is written into their code again.
 *
 * - **Wrong**: the vendor's value differs from the architect's (or GV's rule, or the vendor's own
 *   sum), proved by code; red on the cloud drawing.
 * - **Right**: matches the architect's printed value or GV's rule.
 * - **Not compared**: not reviewed, always with its reason and what is needed.
 * - Round 2 speaks the comment letter's words: **Closed**, **Previous comment not addressed**, **New**.
 * - The product is **GV Review**; it never speaks as another product.
 */

export const OUTCOME_WORDS = { wrong: 'Wrong', right: 'Right', notCompared: 'Not compared' } as const;

/** The composer's placeholder, in a chat with or without messages. */
export const COMPOSER_PLACEHOLDER = 'Ask about these drawings…';
/** #1744: while a full check runs in the chat (the server takes no second message until it ends). */
export const COMPOSER_RUNNING_PLACEHOLDER = 'The check is running. Ask a question in a new chat…';

/** The one line under the composer. */
export const COMPOSER_NOTE = 'GV Review can be wrong. Every red mark shows its proof.';

/** Round 2, as the comment letter says it (decided 2026-10-18; the tracker too, 2026-10-19). */
export const ROUND_WORDS = {
  closed: 'Closed',
  notAddressed: 'Previous comment not addressed',
  new: 'New',
} as const;

/**
 * A tracker tag in the round-2 words. The server tags in these words itself ("Previous comment not
 * addressed", "New", #1766); older answers' "Still wrong" and "New in round N" are mapped, and a
 * first round's "New in round 1" says nothing (every item is new there), so it is not shown. Other
 * tags (the item's kind) pass through.
 */
export function roundTagWords(tag: string): string | null {
  if (/^still wrong$/i.test(tag.trim())) return ROUND_WORDS.notAddressed;
  const round = /^new in round (\d+)$/i.exec(tag.trim());
  if (round) return Number(round[1]) > 1 ? ROUND_WORDS.new : null;
  return tag;
}

const READING = new Set(['UPLOADING', 'UPLOADED', 'INGESTING', 'EXTRACTING', 'MATCHING', 'VALIDATING_EVIDENCE', 'RUNNING_CHECKS', 'GENERATING_OUTPUTS']);

/**
 * A drawing set's state as plain grey words for the sidebar: no pill, no colour (a set's state is
 * not an outcome). Unknown states still read, by their own name. `restarting` is the package's
 * `reading.retrying` (#1249), known only for the open set.
 */
export function setStatusWords(state: string, { restarting = false }: { restarting?: boolean } = {}): string {
  if (READING.has(state)) return 'Reading the drawings…';
  if (state === 'FAILED_RETRYABLE') return restarting ? 'Restarting after a problem' : 'Stopped after a problem';
  switch (state) {
    case 'AWAITING_REVIEW':
    case 'NEEDS_INPUT':
      return 'Drawings read';
    case 'CHANGES_REQUESTED':
      return 'Changes requested';
    case 'APPROVED':
      return 'Approved';
    case 'FAILED_PERMANENT':
      return 'Could not read the drawings';
    case 'CANCELLED':
      return 'Cancelled';
    case 'SUPERSEDED':
      return 'Replaced by a newer upload';
    case 'CREATED':
      return 'Waiting for the drawings';
    default: {
      const words = state.toLowerCase().replace(/_/g, ' ').trim();
      return words.charAt(0).toUpperCase() + words.slice(1);
    }
  }
}

/** Words a reviewer screen must never show, each with what to say instead. */
export const BANNED_WORDS: readonly { pattern: RegExp; instead: string }[] = [
  { pattern: /nothing to compare/i, instead: 'say "Not compared"' },
  { pattern: /reply to claude/i, instead: `say "${COMPOSER_PLACEHOLDER}"` },
  { pattern: /claude can make mistakes/i, instead: `say "${COMPOSER_NOTE}"` },
  { pattern: /how can i help you today/i, instead: `say "${COMPOSER_PLACEHOLDER}"` },
  { pattern: /message claude/i, instead: 'the composer is "Ask GV Review"' },
  { pattern: /\bran (?:a|an|\d+|\$\{[^}]*\}) (?:commands?|agents?)\b/i, instead: 'say what was done in plain words' },
  { pattern: /<\/span>\s*optional\b/, instead: 'no "N optional" count: every question is optional' },
  { pattern: /What[’']s (?:wrong|right)/, instead: 'say "Wrong" / "Right"' },
  { pattern: /what to check(?:,| and)/i, instead: 'there is no "to check": say Wrong / Right / Not compared' },
  { pattern: /your decision/i, instead: 'there is no "Your decision": say "Not compared"' },
  { pattern: /['"`]Still wrong['"`]|Fixed · round|['"`]Fixed in a later round['"`]/, instead: 'say the round-2 words: Closed / Previous comment not addressed / New' },
  // #1756: the product is GV Review; a model is named only by its short name ("Opus 5.5").
  { pattern: /\bClaude\b/, instead: 'say "the AI", or the model\'s short name ("Opus 5.5")' },
];
