/**
 * #1581: the zero-click cloud drawing's groups on the screen: the server's group, the reviewer's
 * newest click laid over it, the rule mirrored for a streaming finding, the CSV's two new columns
 * and the preview page.
 */
import { describe, expect, it } from 'vitest';

import { previewPage } from '@/lib/chat-full-check';
import { asShown, proofCounts, proofGroup, vendorQuestion } from '@/lib/chat-proof';
import { CSV_HEADER, findingsCsv } from '@/lib/chat-trust';
import { finding } from './fixtures/chat';

describe('the zero-click drawing groups (#1581)', () => {
  it('takes the server group and lays the reviewer’s click over it', () => {
    const notVerified = finding('w1', { status: 'different', proof: { group: 'vendor_confirm' } });
    expect(proofGroup(notVerified)).toBe('vendor_confirm');
    expect(proofGroup(notVerified, { decision: 'agree' })).toBe('clouded');
    expect(proofGroup(finding('w2', { status: 'different', proof: { group: 'clouded' } }), { decision: 'not_a_problem' })).toBe('dismissed');
    expect(proofGroup(finding('r1', { status: 'same', proof: { group: 'right' } }), { decision: 'agree' })).toBe('right');
  });

  it('mirrors the rule while an answer streams (no proof yet)', () => {
    const printed = { status: 'different' as const, trust_level: 'confirmed' as const, witnesses: ['printed'], inches: { architect: '37', vendor: '46', difference: '9' }, critic: { result: 'passed' as const, model: "devil's advocate" } };
    expect(proofGroup(finding('a', printed))).toBe('clouded');
    // No proof that both numbers measure the same thing: a question to the vendor (#1619).
    expect(proofGroup(finding('p', { ...printed, critic: null }))).toBe('vendor_confirm');
    expect(proofGroup(finding('q', { ...printed, critic: null, paired_by: 'code' }))).toBe('clouded');
    expect(proofGroup(finding('r', { ...printed, critic: null, source: 'rules', witnesses: ['rule'] }))).toBe('clouded');
    expect(proofGroup(finding('b', { ...printed, witnesses: [] }))).toBe('vendor_confirm');
    expect(proofGroup(finding('c', { ...printed, trust_level: 'to_check' }))).toBe('vendor_confirm');
    expect(proofGroup(finding('d', { ...printed, architect: 'scales about 74"' }))).toBe('vendor_confirm');
    expect(proofGroup(finding('e', { status: 'check' }))).toBe('vendor_confirm');
    expect(proofGroup(finding('f', { status: 'same', compared: false }))).toBe('not_compared');
    expect(proofCounts([finding('a', printed), finding('e', { status: 'check' })])).toEqual({
      clouded: 1, vendor_confirm: 1, right: 0, info: 0, not_compared: 0, dismissed: 0,
    });
  });

  it('adds Should be and Proof to the CSV, and previews the biggest clouded item', () => {
    expect(CSV_HEADER.slice(-2)).toEqual(['Should be', 'Proof']);
    const clouded = finding('w1', { page: 7, status: 'different', proof: { group: 'clouded', should_be: '37" (architect)', proof: 'Printed at both spots, read by code' }, impact: { score: 80, why: 'height' } });
    const grey = finding('w2', { page: 3, status: 'different', proof: { group: 'vendor_confirm', should_be: '2"', reason: 'Not clouded.', question: 'Page 3 shows 3" for the filler. Confirm which size is intended.' }, impact: { score: 90, why: 'size' } });
    const lines = findingsCsv([grey, clouded]).trim().split('\r\n');
    expect(lines[1]).toContain('"Vendor to confirm: Confirm: Page 3 shows 3"" for the filler. Confirm which size is intended."');
    expect(lines[2]).toContain('"Wrong, proved by code (clouded): Printed at both spots, read by code"');
    expect(lines[2]).toContain('"37"" (architect)"');
    expect(previewPage([grey, clouded])).toBe(7);
  });
});

describe('a full check shows each item where its cloud drawing puts it (#1619)', () => {
  it('moves an unproved Wrong to Vendor to confirm and an amber item made Wrong to Wrong', () => {
    const amber = finding('w1', { status: 'different', proof: { group: 'vendor_confirm', question: 'Page 3 shows 46". Confirm which size is intended.' } });
    expect(asShown(amber).status).toBe('check');
    expect(vendorQuestion(asShown(amber))).toBe('Page 3 shows 46". Confirm which size is intended.');
    // "Make it Wrong" is the Agree route: clouded, so Wrong.
    expect(asShown(amber, { decision: 'agree' }).status).toBe('different');
    const asked = finding('c1', { status: 'check', proof: { group: 'vendor_confirm' } });
    expect(asShown(asked, { decision: 'agree' }).status).toBe('different');
    // Not a problem keeps its own status; a proved Wrong stays Wrong; Right stays Right.
    expect(asShown(amber, { decision: 'not_a_problem' }).status).toBe('different');
    expect(asShown(finding('w2', { status: 'different', proof: { group: 'clouded' } })).status).toBe('different');
    expect(asShown(finding('r1', { status: 'same', proof: { group: 'right' } })).status).toBe('same');
    expect(vendorQuestion(finding('r2', { status: 'same', proof: { group: 'right' } }))).toBeNull();
  });
});
