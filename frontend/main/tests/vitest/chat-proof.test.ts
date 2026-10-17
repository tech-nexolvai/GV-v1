/**
 * #1581: the zero-click cloud drawing's groups on the screen: the server's group, the reviewer's
 * newest click laid over it, the rule mirrored for a streaming finding, the CSV's two new columns
 * and the preview page.
 */
import { describe, expect, it } from 'vitest';

import { previewPage } from '@/lib/chat-full-check';
import { proofCounts, proofGroup } from '@/lib/chat-proof';
import { CSV_HEADER, findingsCsv } from '@/lib/chat-trust';
import { finding } from './fixtures/chat';

describe('the zero-click drawing groups (#1581)', () => {
  it('takes the server group and lays the reviewer’s click over it', () => {
    const notVerified = finding('w1', { status: 'different', proof: { group: 'not_verified' } });
    expect(proofGroup(notVerified)).toBe('not_verified');
    expect(proofGroup(notVerified, { decision: 'agree' })).toBe('clouded');
    expect(proofGroup(finding('w2', { status: 'different', proof: { group: 'clouded' } }), { decision: 'not_a_problem' })).toBe('dismissed');
    expect(proofGroup(finding('r1', { status: 'same', proof: { group: 'right' } }), { decision: 'agree' })).toBe('right');
  });

  it('mirrors the rule while an answer streams (no proof yet)', () => {
    const printed = { status: 'different' as const, trust_level: 'confirmed' as const, witnesses: ['printed'], inches: { architect: '37', vendor: '46', difference: '9' }, pairing: { proved: true, method: 'code_geometry' as const } };
    expect(proofGroup(finding('a', printed))).toBe('clouded');
    // No proof that both numbers measure the same thing: grey.
    expect(proofGroup(finding('p', { ...printed, pairing: null }))).toBe('not_verified');
    expect(proofGroup(finding('q', { ...printed, pairing: null, source: 'rules', witnesses: ['rule'] }))).toBe('clouded');
    expect(proofGroup(finding('b', { ...printed, witnesses: [] }))).toBe('not_verified');
    expect(proofGroup(finding('c', { ...printed, trust_level: 'to_check' }))).toBe('not_verified');
    expect(proofGroup(finding('d', { ...printed, architect: 'scales about 74"' }))).toBe('not_verified');
    expect(proofGroup(finding('e', { status: 'check' }))).toBe('could_not_compare');
    expect(proofGroup(finding('f', { status: 'same', compared: false }))).toBe('not_compared');
    expect(proofCounts([finding('a', printed), finding('e', { status: 'check' })])).toEqual({
      clouded: 1, not_verified: 0, could_not_compare: 1, right: 0, not_compared: 0, dismissed: 0,
    });
  });

  it('adds Should be and Proof to the CSV, and previews the biggest clouded item', () => {
    expect(CSV_HEADER.slice(-2)).toEqual(['Should be', 'Proof']);
    const clouded = finding('w1', { page: 7, status: 'different', proof: { group: 'clouded', should_be: '37" (architect)', proof: 'Printed at both spots, read by code' }, impact: { score: 80, why: 'height' } });
    const grey = finding('w2', { page: 3, status: 'different', proof: { group: 'not_verified', should_be: '2"', reason: 'Only AI readings back it.' }, impact: { score: 90, why: 'size' } });
    const lines = findingsCsv([grey, clouded]).trim().split('\r\n');
    expect(lines[1]).toContain(',Not verified by code: check before sending: Only AI readings back it.');
    expect(lines[2]).toContain('"Wrong, proved by code (clouded): Printed at both spots, read by code"');
    expect(lines[2]).toContain('"37"" (architect)"');
    expect(previewPage([grey, clouded])).toBe(7);
  });
});
