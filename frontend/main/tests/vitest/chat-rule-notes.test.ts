/**
 * #1839: a size the architect specifies that breaks a GV rule, printed the same by the vendor:
 * decided, its own group "Ask the architect", never red, never Not compared. Made-up values only.
 */
import { describe, expect, it } from 'vitest';

import type { ChatFinding, FindingProof } from '@/lib/chat-api';
import { proofCounts, proofGroup, ruleNoteWords } from '@/lib/chat-proof';
import { resultGroups, resultTabs } from '@/lib/chat-result';

const NOTE: FindingProof = {
  group: 'rule_note',
  reason: 'the architect specifies 5/8" for the filler width; GV\'s standard is 1"-2"',
  label: 'Ask the architect (GV rule)',
  rule_note: {
    name: 'filler width',
    settings: '1"-2"',
    origin: 'GV standard',
    architect: '5/8"',
    standard: 'GV\'s standard is 1"-2"',
    question: 'Architect: the filler width is specified at 5/8" (Pantry); GV\'s standard is 1"-2". Please confirm the size.',
  },
};

const finding = (id: string, extra: Partial<ChatFinding> = {}): ChatFinding =>
  ({ id, page: 2, item: `Pantry: filler ${id}`, architect: '5/8" FILLER', vendor: '5/8" FILLER', status: 'check', why: '', verified: false, source: 'architect', ...extra }) as ChatFinding;

describe('rule notes (#1839)', () => {
  it('get their own group and tile, apart from Not compared', () => {
    const findings = [
      finding('a', { proof: NOTE }),
      finding('b', { proof: { group: 'not_compared', not_compared: 'unreadable' } }),
      finding('c', { status: 'different', proof: { group: 'clouded' } }),
    ];
    const groups = resultGroups(findings);
    expect(groups.ruleNotes.map((f) => f.id)).toEqual(['a']);
    expect(groups.notCompared.map((f) => f.id)).toEqual(['b']);
    // Results v2 (2026-10-23): all six tiles, always in the same place.
    expect(resultTabs()).toEqual(['wrong', 'likely_problem', 'rule_note', 'drawing_needed', 'right', 'not_compared']);
    expect(proofCounts(findings)).toMatchObject({ clouded: 1, rule_note: 1, not_compared: 1 });
  });

  it('say the question for the architect, and follow the reviewer’s call and its undo', () => {
    expect(ruleNoteWords(finding('a', { proof: NOTE }))?.question).toMatch(/^Architect: the filler width is specified at 5\/8"/);
    const agreed = finding('a', { proof: { ...NOTE, group: 'clouded' }, decision: { decision: 'agree', should_be: '1"' } as ChatFinding['decision'] });
    expect(proofGroup(agreed)).toBe('clouded');
    expect(ruleNoteWords(agreed)).toBeNull();
    // Undone (no decision any more): back to what code found.
    expect(proofGroup(agreed, null)).toBe('rule_note');
    expect(proofGroup(finding('a', { proof: NOTE }), { decision: 'not_a_problem' } as ChatFinding['decision'])).toBe('dismissed');
  });
});
