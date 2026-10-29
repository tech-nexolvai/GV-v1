/**
 * The page reviewer's please-check kinds (2026-10-29): construction, drawing completeness, material,
 * scope and labels. A card the server has not grouped yet (a live answer) still names its kind in
 * words, never "A size differs". Made-up values only.
 */
import { describe, expect, it } from 'vitest';

import type { ChatFinding, FindingLikelyProblem } from '@/lib/chat-api';
import { referenceSide, vendorOnly, vendorSide } from '@/lib/chat-drawer';
import { likelyGroups, questionOf } from '@/lib/chat-result';
import { finding } from './fixtures/chat';

function reviewed(id: string, kind: FindingLikelyProblem['kind']): ChatFinding {
  return finding(id, {
    item: 'Island top: overhang support',
    status: 'check',
    created_by: 'page_reviewer',
    arch_picture_id: null,
    page: 4,
    architect: 'n/a',
    vendor: 'see the drawing',
    proof: {
      group: 'likely_problem',
      reason: 'The top overhangs 14 in; no support is shown.',
      likely_problem: { kind, words: 'Please check and confirm: Island top: no support under the overhang.', reason: null, sure: 'likely', by: 'page_reviewer' },
    },
  });
}

describe('the page reviewer kinds', () => {
  it.each([
    ['construction', 'Construction: a support, filler or panel to check'],
    ['completeness', 'Drawing completeness: a view, size or mark to add or remove'],
    ['finish', 'Material, finish or grain callout to check'],
    ['scope', 'Scope: who supplies it (GV, the GC or the owner) to make clear'],
    ['labels', 'Item number, label or title block to check'],
  ] as const)('%s is named in words', (kind, words) => {
    expect(questionOf(reviewed('p1', kind))).toEqual({ key: `look:${kind}`, words });
  });

  it('groups the same kind together and keeps kinds apart', () => {
    const groups = likelyGroups([reviewed('p1', 'construction'), reviewed('p2', 'scope'), reviewed('p3', 'construction')]);
    expect(groups.map((group) => [group.key, group.findings.map((each) => each.id).sort()])).toEqual([
      ['look:construction', ['p1', 'p3']],
      ['look:scope', ['p2']],
    ]);
  });
});

describe('the page reviewer item in the drawer', () => {
  it('is the vendor page’s own: no architect side, never "n/a"', () => {
    const item = reviewed('p1', 'construction');
    expect(vendorOnly(item)).toBe(true);
    expect(referenceSide(item, 'likely_problem')).toEqual({ label: 'Where on the vendor’s page', value: 'p.4', tone: null });
    expect(vendorSide(item, 'likely_problem').value).toBe('p.4');
  });

  it('leaves every other item as it was', () => {
    const helper = finding('h1', { status: 'check', architect: '24"', vendor: '25"' });
    expect(vendorOnly(helper)).toBe(false);
    expect(referenceSide(helper, 'not_compared').label).toBe('Architect shows');
  });
});

describe('the same tag at other spots of its own page (2026-10-29)', () => {
  it('says "more spots on this page"; other pages stay "Also on"', async () => {
    const { alsoOnWords } = await import('@/lib/chat-findings');
    const box = { page: 3, x0: 10, y0: 34, x1: 200, y1: 44 };
    const alsoOn = [
      { finding_id: 'F9', page: 3, vendor: 'ST-03', vendor_box: box },
      { finding_id: 'F9', page: 3, vendor: 'ST-03', vendor_box: { ...box, y0: 70, y1: 80 } },
      { finding_id: 'F9', page: 5, vendor: 'ST-03', vendor_box: { ...box, page: 5 } },
    ];
    expect(alsoOnWords({ page: 3, also_on: alsoOn })).toBe('Also on p.5 and 2 more spots on this page');
    expect(alsoOnWords({ page: 3, also_on: alsoOn.slice(0, 1) })).toBe('1 more spot on this page');
    expect(alsoOnWords({ page: 3, also_on: [] })).toBe('');
  });
});
