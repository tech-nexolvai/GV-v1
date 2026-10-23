/**
 * #1581, #1619: the zero-click cloud drawing's rule, as the screen counts it. The architect's drawings
 * govern (Anant's rules R1-R10, `classification.py`): an item is Wrong (red, proved by code: clouded),
 * Right, or Not compared, with its reason and what is needed, listed apart in the Not-compared PDF and
 * workbook and never a question to anyone. The reviewer's decisions are optional overrides (#1803):
 * on a Not-compared item "It's a mistake: add a red mark" (an Agree, with the value it should be)
 * or "It's fine: mark Right" (Not a problem); on a Wrong item "Not a mistake: remove the red mark"
 * (Not a problem). Marked Right shows as "Right (reviewer)".
 *
 * The server sends each stored finding's group (`finding.proof`, worked out by `proof.py`); this
 * file only lays the reviewer's newest click over it, and mirrors the rule for a finding still
 * streaming (no `proof` yet), so the card's numbers match the drawing's cover.
 */

import type { ChatFinding, FindingDecision, FindingStatus, ProofGroup } from '@/lib/chat-api';

export const PROOF_GROUPS: readonly ProofGroup[] = ['clouded', 'right', 'rule_note', 'likely_problem', 'not_compared', 'dismissed'];

export type ProofCounts = Record<ProofGroup, number>;

const PROVED_LEVELS = new Set(['confirmed', 'likely']);
const PAIRED_SOURCES = new Set(['architect', 'brand_prototype', 'gv_review']);
const SCALED = /\bscal(?:e|es|ed|ing)\b|\bmeasured (?:off|on) the drawing\b/i;

/** Internal words that never reach the vendor (#1622, `vendor_text.py`): "see GV Review's record". */
const INTERNAL = /GV Review'?s record|recorded check/i;

/**
 * #1622 (`proof.stated_requirement`): the vendor can be told what the value should be: a size code
 * read, or the governing value with a number and no internal words (or a finish code read from the
 * drawing's text). A comment without one is never sent, whoever agreed it.
 */
function statedRequirement(finding: ChatFinding, decision: FindingDecision | null = null): boolean {
  // #1803: the reviewer said what it should be (the reviewer is the witness).
  if (decision?.decision === 'agree' && decision.should_be?.trim() && !INTERNAL.test(decision.should_be)) return true;
  if (finding.inches?.architect != null || finding.settled?.method === 'gv_limits' || finding.source === 'vendor_arithmetic') return true;
  const value = finding.architect ?? '';
  if (!value || INTERNAL.test(value)) return false;
  return /\d/.test(value) || (finding.witnesses ?? []).includes('text');
}

/** The rule for a finding with no `proof` from the server yet (a streaming answer). */
function codeProved(finding: ChatFinding): boolean {
  if (finding.status !== 'different') return false;
  if (finding.check_reason === 'scaled' || SCALED.test(finding.architect ?? '') || SCALED.test(finding.vendor ?? '')) return false;
  const witnesses = finding.witnesses ?? [];
  if (!PROVED_LEVELS.has(finding.trust_level ?? '') || witnesses.length === 0) return false;
  if (finding.settled?.method === 'gv_limits') return true;
  if (witnesses.includes('rule')) return statedRequirement(finding);
  if (finding.source === 'vendor_arithmetic' && witnesses.includes('arithmetic')) return true;
  // An architect comparison also needs proof that both numbers measure the same thing: the full
  // check's one signal (#1611): code paired the two itself, or its devil's advocate passed it.
  const needsPairing = PAIRED_SOURCES.has(finding.source) && !finding.rule_check_id && finding.paired_by !== 'code';
  const paired = finding.critic?.model === "devil's advocate" && finding.critic.result === 'passed';
  return finding.inches?.architect != null && (!needsPairing || paired);
}

/** Where the zero-click drawing puts a finding, with the reviewer's newest click laid over. */
export function proofGroup(finding: ChatFinding, decision: FindingDecision | null = finding.decision ?? null): ProofGroup {
  // #1652: an item for GV's reviewer only (read by the AI, never at its spot) is never clouded.
  if (isInternal(finding)) return 'not_compared';
  const base = codeGroup(finding);
  // A compared Right has nothing to override.
  if (base === 'right') return 'right';
  // The reviewer's decision on any other item (#1803: a Not-compared "same" too). An Agree is
  // clouded when the vendor can be told what it should be (the architect's value, or the reviewer's).
  if (decision?.decision === 'agree' && statedRequirement(finding, decision)) return 'clouded';
  if (decision?.decision === 'not_a_problem') return 'dismissed';
  return base;
}

/** Where code puts the item, before any decision: the server's group (with its own decision taken off), else the rule. */
function codeGroup(finding: ChatFinding): ProofGroup {
  const server = finding.proof?.group;
  // The server's group already holds the stored decision: an overridden item goes back to what code found.
  if (server && server !== 'dismissed' && !(server === 'clouded' && finding.decision)) return server;
  if (finding.status === 'different' && codeProved(finding)) return 'clouded';
  // #1839: the architect's size breaks a GV rule and the vendor follows it (the server's words
  // stay on the item under an override): decided, GV asks the architect.
  if (finding.proof?.rule_note && finding.status === 'check') return 'rule_note';
  // What code could not prove or compare is Not compared (#1619): listed apart, never asked.
  if (finding.status === 'different' || finding.status === 'check') return 'not_compared';
  return finding.compared === false ? 'not_compared' : 'right';
}

/**
 * A full check's finding as its result screen shows it (#1619): in the section of its place on the
 * cloud drawing, with the reviewer's newest click laid over: clouded = Wrong, Right, and Not compared
 * (status `check`, the folded "Nothing to compare" section). What the reviewer marked not a problem
 * keeps its own status (its row shows the click).
 */
export function asShown(finding: ChatFinding, decision: FindingDecision | null = finding.decision ?? null): ChatFinding {
  const group = proofGroup(finding, decision);
  const status: FindingStatus | null = group === 'clouded' ? 'different' : group === 'right' ? 'same' : group === 'not_compared' || group === 'rule_note' || group === 'likely_problem' ? 'check' : null;
  return status === null || status === finding.status ? finding : { ...finding, status };
}

/** Why a Not-compared item could not be compared: the trade's words, ours, and what is needed. */
export interface NotComparedWords {
  label: string;
  why: string | null;
  settle: string | null;
}

/**
 * #1839: a size the architect specifies that breaks a GV rule, printed the same by the vendor:
 * decided (never red, never "not compared"); GV asks the architect. The server's words.
 */
export interface RuleNoteWords {
  why: string | null;
  /** "GV’s standard is 1″-2″". */
  standard: string;
  /** The ready question for the architect. */
  question: string;
}

export function ruleNoteWords(finding: ChatFinding, decision: FindingDecision | null = finding.decision ?? null): RuleNoteWords | null {
  const note = finding.proof?.rule_note;
  if (!note || proofGroup(finding, decision) !== 'rule_note') return null;
  return { why: finding.proof?.reason?.trim() || null, standard: note.standard, question: note.question };
}

export function notComparedWords(finding: ChatFinding): NotComparedWords | null {
  const proof = finding.proof;
  if (proof?.group !== 'not_compared') return null;
  return { label: proof.label?.trim() || 'Not reviewed', why: proof.reason?.trim() || null, settle: proof.settle?.trim() || null };
}

/**
 * #1652: an item for GV's reviewer only: a helper read it but never recorded it at its spot, or one
 * drawing shows it and code never searched the other. Listed on the screen in grey, never sent: not
 * on the cloud drawing, the letter, the register or the Not-compared list, and not counted with them.
 */
export function isInternal(finding: ChatFinding): boolean {
  return finding.proof?.not_compared === 'internal';
}

/** How many items each group has (the drawing's cover counts the same final list: internal items are not on it). */
export function proofCounts(findings: readonly ChatFinding[], decisionOf?: (finding: ChatFinding) => FindingDecision | null): ProofCounts {
  const counts = Object.fromEntries(PROOF_GROUPS.map((group) => [group, 0])) as ProofCounts;
  for (const finding of findings) {
    if (isInternal(finding)) continue;
    counts[proofGroup(finding, decisionOf ? decisionOf(finding) : finding.decision ?? null)] += 1;
  }
  return counts;
}

export const PROOF_WORDS: Record<ProofGroup, string> = {
  clouded: 'Wrong, proved by code (clouded)',
  right: 'Right',
  rule_note: 'Breaks a GV rule (architect’s size): ask the architect',
  likely_problem: 'Likely problem: please check and confirm',
  not_compared: 'Not compared',
  dismissed: 'Right (reviewer)',
};
