/**
 * #1581: the zero-click cloud drawing's rule, as the screen counts it. A full check ends with a
 * cloud drawing made by code with no clicks: only the Wrong items code proved are clouded (red:
 * the vendor's value, green: what it should be); Wrong items only AI readings back are listed in
 * grey ("not verified by code"); "Your decision" items are listed as "could not compare". The
 * reviewer's Agree / Not a problem are optional overrides: agreed = clouded, not a problem = left
 * out.
 *
 * The server sends each stored finding's group (`finding.proof`, worked out by `proof.py`); this
 * file only lays the reviewer's newest click over it, and mirrors the rule for a finding still
 * streaming (no `proof` yet), so the card's numbers match the drawing's cover.
 */

import type { ChatFinding, FindingDecision, ProofGroup } from '@/lib/chat-api';

export const PROOF_GROUPS: readonly ProofGroup[] = ['clouded', 'not_verified', 'could_not_compare', 'right', 'not_compared', 'dismissed'];

export type ProofCounts = Record<ProofGroup, number>;

const PROVED_LEVELS = new Set(['confirmed', 'likely']);
const SCALED = /\bscal(?:e|es|ed|ing)\b|\bmeasured (?:off|on) the drawing\b/i;

/** The rule for a finding with no `proof` from the server yet (a streaming answer). */
function codeProved(finding: ChatFinding): boolean {
  if (finding.status !== 'different') return false;
  if (finding.check_reason === 'scaled' || SCALED.test(finding.architect ?? '') || SCALED.test(finding.vendor ?? '')) return false;
  const witnesses = finding.witnesses ?? [];
  if (!PROVED_LEVELS.has(finding.trust_level ?? '') || witnesses.length === 0) return false;
  if (witnesses.includes('rule') || finding.settled?.method === 'gv_limits') return true;
  if (finding.source === 'vendor_arithmetic' && witnesses.includes('arithmetic')) return true;
  return finding.inches?.architect != null;
}

/** Where the zero-click drawing puts a finding, with the reviewer's newest click laid over. */
export function proofGroup(finding: ChatFinding, decision: FindingDecision | null = finding.decision ?? null): ProofGroup {
  if (finding.status === 'different' || finding.status === 'check') {
    if (decision?.decision === 'agree') return 'clouded';
    if (decision?.decision === 'not_a_problem') return 'dismissed';
  }
  // With no click to lay over, the server's group stands.
  if (finding.proof?.group) return finding.proof.group;
  if (finding.status === 'different') return codeProved(finding) ? 'clouded' : 'not_verified';
  if (finding.status === 'check') return 'could_not_compare';
  return finding.compared === false ? 'not_compared' : 'right';
}

/** How many items each group has (the drawing's cover counts the same final list). */
export function proofCounts(findings: readonly ChatFinding[], decisionOf?: (finding: ChatFinding) => FindingDecision | null): ProofCounts {
  const counts = Object.fromEntries(PROOF_GROUPS.map((group) => [group, 0])) as ProofCounts;
  for (const finding of findings) counts[proofGroup(finding, decisionOf ? decisionOf(finding) : finding.decision ?? null)] += 1;
  return counts;
}

export const PROOF_WORDS: Record<ProofGroup, string> = {
  clouded: 'Wrong, proved by code (clouded)',
  not_verified: 'Not verified by code: check before sending',
  could_not_compare: 'Could not be compared',
  right: 'Right',
  not_compared: 'Not compared',
  dismissed: 'Not a problem (reviewer)',
};
