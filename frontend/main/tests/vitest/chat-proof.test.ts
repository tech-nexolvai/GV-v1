/**
 * #1581: the zero-click cloud drawing's groups on the screen: the server's group, the reviewer's
 * newest click laid over it, the rule mirrored for a streaming finding, the CSV's two new columns
 * and the preview page.
 */
import { describe, expect, it } from 'vitest';

import { previewPage } from '@/lib/chat-full-check';
import { asShown, notComparedWords, PROOF_WORDS, proofCounts, proofGroup } from '@/lib/chat-proof';
import { shouldBeValue, suggestedShouldBe } from '@/lib/chat-result';
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
      clouded: 1, right: 0, rule_note: 0, likely_problem: 0, not_compared: 1, dismissed: 0,
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
    expect(proofCounts([note, sent])).toEqual({ clouded: 1, right: 0, rule_note: 0, likely_problem: 0, not_compared: 0, dismissed: 0 });
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

describe('the reviewer’s overrides (#1803)', () => {
  it('a red mark needs a value it should be; the reviewer can give one; a compared Right is never overridden', () => {
    const none = finding('k1', { status: 'check', architect: 'no size printed', inches: null, proof: { group: 'not_compared' } });
    expect(proofGroup(none, { decision: 'agree' })).toBe('not_compared');
    expect(proofGroup(none, { decision: 'agree', should_be: '12"' })).toBe('clouded');
    expect(shouldBeValue(none, { decision: 'agree', should_be: '12"' })).toEqual({ main: '12″', note: 'the reviewer' });
    expect(suggestedShouldBe(none)).toBe('');
    // A "same" seen on one side only is Not compared: it can be marked Right or made Wrong.
    const unseen = finding('s1', { status: 'same', compared: false, architect: '22"', vendor: '22"', proof: { group: 'not_compared' } });
    expect(proofGroup(unseen, { decision: 'not_a_problem' })).toBe('dismissed');
    expect(proofGroup(unseen, { decision: 'agree', should_be: '24"' })).toBe('clouded');
    expect(proofGroup(finding('r1', { status: 'same', proof: { group: 'right' } }), { decision: 'not_a_problem' })).toBe('right');
    // The architect's value starts the form, as code writes it; kept as it is, it is the architect's.
    const architect = finding('a1', { status: 'different', architect: '3\'-1"', inches: { architect: '37', vendor: '46', difference: '9' }, proof: { group: 'not_compared' } });
    expect(suggestedShouldBe(architect)).toBe('37"');
    expect(shouldBeValue(architect, { decision: 'agree', should_be: '37 ″' })?.note).not.toBe('the reviewer');
    expect(PROOF_WORDS.dismissed).toBe('Right (reviewer)');
  });
});
