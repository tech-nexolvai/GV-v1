/**
 * #1581, #1619: the zero-click cloud drawing's rule, as the screen counts it. A full check ends with
 * a cloud drawing made by code with no clicks: only the Wrong items code proved are clouded (red:
 * the vendor's value, green: what it should be). What code could not settle, after its escalation,
 * becomes ONE question to the vendor ("Vendor to confirm", amber: a marker and a "Confirm: …" note,
 * never a cloud); an item with nothing to ask is "Not applicable / no information". The reviewer's
 * Agree / Not a problem / Make it Wrong are optional overrides: agreed = clouded, not a problem =
 * left out.
 *
 * The server sends each stored finding's group (`finding.proof`, worked out by `proof.py`); this
 * file only lays the reviewer's newest click over it, and mirrors the rule for a finding still
 * streaming (no `proof` yet), so the card's numbers match the drawing's cover.
 */

import type { ChatFinding, FindingDecision, FindingStatus, ProofGroup } from '@/lib/chat-api';

export const PROOF_GROUPS: readonly ProofGroup[] = ['clouded', 'vendor_confirm', 'right', 'info', 'not_compared', 'dismissed'];

export type ProofCounts = Record<ProofGroup, number>;

const PROVED_LEVELS = new Set(['confirmed', 'likely']);
const PAIRED_SOURCES = new Set(['architect', 'brand_prototype', 'gv_review']);
const SCALED = /\bscal(?:e|es|ed|ing)\b|\bmeasured (?:off|on) the drawing\b/i;

/** The rule for a finding with no `proof` from the server yet (a streaming answer). */
function codeProved(finding: ChatFinding): boolean {
  if (finding.status !== 'different') return false;
  if (finding.check_reason === 'scaled' || SCALED.test(finding.architect ?? '') || SCALED.test(finding.vendor ?? '')) return false;
  const witnesses = finding.witnesses ?? [];
  if (!PROVED_LEVELS.has(finding.trust_level ?? '') || witnesses.length === 0) return false;
  if (witnesses.includes('rule') || finding.settled?.method === 'gv_limits') return true;
  if (finding.source === 'vendor_arithmetic' && witnesses.includes('arithmetic')) return true;
  // An architect comparison also needs proof that both numbers measure the same thing: the full
  // check's one signal (#1611): code paired the two itself, or its devil's advocate passed it.
  const needsPairing = PAIRED_SOURCES.has(finding.source) && !finding.rule_check_id && finding.paired_by !== 'code';
  const paired = finding.critic?.model === "devil's advocate" && finding.critic.result === 'passed';
  return finding.inches?.architect != null && (!needsPairing || paired);
}

/** Where the zero-click drawing puts a finding, with the reviewer's newest click laid over. */
export function proofGroup(finding: ChatFinding, decision: FindingDecision | null = finding.decision ?? null): ProofGroup {
  if (finding.status === 'different' || finding.status === 'check') {
    if (decision?.decision === 'agree') return 'clouded';
    if (decision?.decision === 'not_a_problem') return 'dismissed';
  }
  // With no click to lay over, the server's group stands.
  if (finding.proof?.group) return finding.proof.group;
  if (finding.status === 'different' && codeProved(finding)) return 'clouded';
  // Undecided: a question to the vendor (the server writes it; a streaming item has none yet).
  if (finding.status === 'different' || finding.status === 'check') return 'vendor_confirm';
  return finding.compared === false ? 'not_compared' : 'right';
}

/**
 * A full check's finding as its result screen shows it (#1619): in the section of its place on the
 * cloud drawing, with the reviewer's newest click laid over: clouded = Wrong, a question to the
 * vendor (or nothing to ask) = Vendor to confirm, Right. What the reviewer marked not a problem
 * keeps its own status (its row shows the click).
 */
export function asShown(finding: ChatFinding, decision: FindingDecision | null = finding.decision ?? null): ChatFinding {
  const group = proofGroup(finding, decision);
  const status: FindingStatus | null = group === 'clouded' ? 'different' : group === 'vendor_confirm' || group === 'info' ? 'check' : group === 'right' || group === 'not_compared' ? 'same' : null;
  return status === null || status === finding.status ? finding : { ...finding, status };
}

/** The question to the vendor for an amber item, from the server; null when it has none. */
export function vendorQuestion(finding: ChatFinding): string | null {
  return finding.proof?.group === 'vendor_confirm' ? finding.proof.question?.trim() || null : null;
}

/** How many items each group has (the drawing's cover counts the same final list). */
export function proofCounts(findings: readonly ChatFinding[], decisionOf?: (finding: ChatFinding) => FindingDecision | null): ProofCounts {
  const counts = Object.fromEntries(PROOF_GROUPS.map((group) => [group, 0])) as ProofCounts;
  for (const finding of findings) counts[proofGroup(finding, decisionOf ? decisionOf(finding) : finding.decision ?? null)] += 1;
  return counts;
}

export const PROOF_WORDS: Record<ProofGroup, string> = {
  clouded: 'Wrong, proved by code (clouded)',
  vendor_confirm: 'Vendor to confirm',
  right: 'Right',
  info: 'Not applicable / no information',
  not_compared: 'Not compared',
  dismissed: 'Not a problem (reviewer)',
};
