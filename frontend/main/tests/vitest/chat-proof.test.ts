/**
 * #1581: the zero-click cloud drawing's groups on the screen: the server's group, the reviewer's
 * newest click laid over it, the rule mirrored for a streaming finding, the CSV's two new columns
 * and the preview page.
 */
import { describe, expect, it } from 'vitest';

import { previewPage } from '@/lib/chat-full-check';
import { asShown, notComparedWords, proofCounts, proofGroup } from '@/lib/chat-proof';
import { CSV_HEADER, findingsCsv } from '@/lib/chat-trust';
import { finding } from './fixtures/chat';

describe('the zero-click drawing groups (#1581)', () => {
  it('takes the server group and lays the reviewer’s click over it', () => {
    const notVerified = finding('w1', { status: 'different', proof: { group: 'not_compared' } });
    expect(proofGroup(notVerified)).toBe('not_compared');
    expect(proofGroup(notVerified, { decision: 'agree' })).toBe('clouded');
    expect(proofGroup(finding('w2', { status: 'different', proof: { group: 'clouded' } }), { decision: 'not_a_problem' })).toBe('dismissed');
    expect(proofGroup(finding('r1', { status: 'same', proof: { group: 'right' } }), { decision: 'agree' })).toBe('right');
  });

  it('mirrors the rule while an answer streams (no proof yet)', () => {
    const printed = { status: 'different' as const, trust_level: 'confirmed' as const, witnesses: ['printed'], inches: { architect: '37', vendor: '46', difference: '9' }, critic: { result: 'passed' as const, model: "devil's advocate" } };
    expect(proofGroup(finding('a', printed))).toBe('clouded');
    // No proof that both numbers measure the same thing: Not compared (#1619).
    expect(proofGroup(finding('p', { ...printed, critic: null }))).toBe('not_compared');
    expect(proofGroup(finding('q', { ...printed, critic: null, paired_by: 'code' }))).toBe('clouded');
    expect(proofGroup(finding('r', { ...printed, critic: null, source: 'rules', witnesses: ['rule'] }))).toBe('clouded');
    expect(proofGroup(finding('b', { ...printed, witnesses: [] }))).toBe('not_compared');
    expect(proofGroup(finding('c', { ...printed, trust_level: 'to_check' }))).toBe('not_compared');
    expect(proofGroup(finding('d', { ...printed, architect: 'scales about 74"' }))).toBe('not_compared');
    expect(proofGroup(finding('e', { status: 'check' }))).toBe('not_compared');
    expect(proofGroup(finding('f', { status: 'same', compared: false }))).toBe('not_compared');
    expect(proofCounts([finding('a', printed), finding('e', { status: 'check' })])).toEqual({
      clouded: 1, right: 0, not_compared: 1, dismissed: 0,
    });
  });

  it('adds Should be and Proof to the CSV, and previews the biggest clouded item', () => {
    expect(CSV_HEADER.slice(-2)).toEqual(['Should be', 'Proof']);
    const clouded = finding('w1', { page: 7, status: 'different', proof: { group: 'clouded', should_be: '37" (architect)', proof: 'Printed at both spots, read by code' }, impact: { score: 80, why: 'height' } });
    const grey = finding('w2', { page: 3, status: 'different', proof: { group: 'not_compared', reason: 'could not read the vendor’s value on page 3', label: 'Illegible', settle: 'a legible value on page 3' }, impact: { score: 90, why: 'size' } });
    const lines = findingsCsv([grey, clouded]).trim().split('\r\n');
    expect(lines[1]).toContain('Not compared: Illegible: could not read the vendor’s value on page 3. Needed: a legible value on page 3');
    expect(lines[2]).toContain('"Wrong, proved by code (clouded): Printed at both spots, read by code"');
    expect(lines[2]).toContain('"37"" (architect)"');
    expect(previewPage([grey, clouded])).toBe(7);
  });
});

describe('items for GV’s reviewer only (#1652)', () => {
  it('are never clouded, even when agreed, and are not counted with what is sent', () => {
    const note = finding('n1', { status: 'check', proof: { group: 'not_compared', not_compared: 'internal', label: 'Internal: read but not recorded at its spot' } });
    expect(proofGroup(note)).toBe('not_compared');
    expect(proofGroup(note, { decision: 'agree' })).toBe('not_compared');
    expect(notComparedWords(note)?.label).toBe('Internal: read but not recorded at its spot');
    const sent = finding('w1', { status: 'different', proof: { group: 'clouded' } });
    expect(proofCounts([note, sent])).toEqual({ clouded: 1, right: 0, not_compared: 0, dismissed: 0 });
  });
});

describe('a full check shows each item where its cloud drawing puts it (#1619)', () => {
  it('puts what code could not prove under Nothing to compare, and Make it Wrong moves it to Wrong', () => {
    const open = finding('w1', { status: 'different', proof: { group: 'not_compared', label: 'Not reviewed: not directly comparable', reason: 'the two dimensions run between different points', settle: 'the printed offset' } });
    expect(asShown(open).status).toBe('check');
    expect(notComparedWords(asShown(open))).toEqual({ label: 'Not reviewed: not directly comparable', why: 'the two dimensions run between different points', settle: 'the printed offset' });
    // "Make it Wrong" is the Agree route: clouded, so Wrong.
    expect(asShown(open, { decision: 'agree' }).status).toBe('different');
    // Not a problem keeps its own status; a proved Wrong stays Wrong; Right stays Right.
    expect(asShown(open, { decision: 'not_a_problem' }).status).toBe('different');
    expect(asShown(finding('w2', { status: 'different', proof: { group: 'clouded' } })).status).toBe('different');
    expect(asShown(finding('r1', { status: 'same', proof: { group: 'right' } })).status).toBe('same');
    expect(notComparedWords(finding('r2', { status: 'same', proof: { group: 'right' } }))).toBeNull();
  });
});
