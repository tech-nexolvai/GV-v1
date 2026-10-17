/**
 * Chat v2/v3 (#1483) without React: findings grouped, filtered and counted; updates and drops by id;
 * verified and source words; coverage counts; short answers; trust levels, checks, code's inches,
 * rounds and the CSV export. Synthetic data only.
 */
import { describe, expect, it } from 'vitest';

import {
  areaOf,
  categoriesPresent,
  countWords,
  coverageCounts,
  coveragePageWords,
  coverageWords,
  findingCounts,
  groupByArea,
  groupCountWords,
  groupFindings,
  isShortAnswer,
  NO_FILTER,
  pagesWords,
  pageWords,
  findingLine,
  isNarrowAnswer,
  itemTitle,
  reviewerWords,
  shortValue,
  spotInsets,
  sureWords,
  shownInOrder,
  shownValue,
  sourceTag,
  verifiedWords,
} from '@/lib/chat-findings';
import { applyChatEvent, fromSaved, localAnswer } from '@/lib/chat-messages';
import {
  changeWords,
  checkCounts,
  checkLines,
  csvCell,
  findingChecks,
  findingsCsv,
  inchWords,
  levelCounts,
  roundCounts,
  roundWords,
  sideValue,
  trustReason,
} from '@/lib/chat-trust';
import type { ChatFinding } from '@/lib/chat-api';
import { finding } from './fixtures/chat';

const SET = [
  finding('f1', { category: 'cabinet', page: 7, status: 'different' }),
  finding('f2', { category: 'countertop', page: 9, status: 'check' }),
  finding('f3', { category: 'countertop', page: 3, status: 'different' }),
  finding('f4', { category: 'other', page: 2, status: 'same' }),
  finding('f5', { category: 'countertop', page: 3, status: 'same' }),
];

describe('grouping, filtering and counts', () => {
  it('groups Countertops, Cabinets, Other, each by page, and hides Same unless asked', () => {
    const groups = groupFindings(SET, NO_FILTER);
    expect(groups.map((group) => group.category)).toEqual(['countertop', 'cabinet']);
    expect(groups[0].findings.map((item) => item.id)).toEqual(['f3', 'f2']);
    expect(shownInOrder(groups).map((item) => item.id)).toEqual(['f3', 'f2', 'f1']);

    const same = groupFindings(SET, { ...NO_FILTER, status: 'same' });
    expect(shownInOrder(same).map((item) => item.id)).toEqual(['f5', 'f4']);
  });

  it('filters by status, category and page', () => {
    expect(shownInOrder(groupFindings(SET, { ...NO_FILTER, status: 'different' })).map((item) => item.id)).toEqual(['f3', 'f1']);
    expect(shownInOrder(groupFindings(SET, { ...NO_FILTER, category: 'cabinet' })).map((item) => item.id)).toEqual(['f1']);
    expect(shownInOrder(groupFindings(SET, { ...NO_FILTER, page: 9 })).map((item) => item.id)).toEqual(['f2']);
    // A page filter also finds a finding by the page it was compared with.
    expect(shownInOrder(groupFindings([finding('x', { page: 4, vs_page: 40 })], { ...NO_FILTER, page: 40 }))).toHaveLength(1);
    expect(categoriesPresent(SET)).toEqual(['countertop', 'cabinet', 'other']);
  });

  it('counts in plain words', () => {
    const counts = findingCounts(SET);
    expect(counts).toEqual({ different: 2, check: 1, same: 2, not_compared: 0, total: 5 });
    expect(countWords(counts)).toBe('2 wrong · 1 needs your decision');
    expect(countWords({ different: 1, check: 0 })).toBe('1 wrong');
    expect(countWords({ different: 0, check: 3 })).toBe('Nothing wrong · 3 need your decision');
  });

  it('shows values verbatim, a dash for a blank, and the page pair', () => {
    expect(shownValue('3\'-1"')).toBe('3\'-1"');
    expect(shownValue('  ')).toBe('—');
    expect(shownValue(null)).toBe('—');
    expect(pageWords({ page: 4, vs_page: 40 })).toBe('4 vs 40');
    expect(pageWords({ page: 4, vs_page: 4 })).toBe('4');
  });

  it('says what the tick and the source mean', () => {
    expect(verifiedWords(true)).toBe('Checked on the drawing');
    expect(verifiedWords(false)).toBe('Helper lead, not checked');
    expect(sourceTag({ source: 'architect' })).toBeNull();
    expect(sourceTag({ source: 'brand_prototype' })).toBe('vs brand prototype');
    expect(sourceTag({ source: 'vendor_arithmetic' })).toBe('vendor’s own string');
    expect(sourceTag({ source: 'rules' })).toBe('GV rule');
  });
});

describe('findings arriving, updated and dropped by id', () => {
  it('adds, replaces in place and removes, and the stored message wins at the end', () => {
    let message = localAnswer('k', { model: 'claude-opus-5-5', effort: 'high', thinking: true });
    message = applyChatEvent(message, { event: 'finding', data: { finding: finding('a', { vendor: '30"' }) } });
    message = applyChatEvent(message, { event: 'finding', data: { finding: finding('b') } });
    message = applyChatEvent(message, { event: 'finding', data: { finding: finding('a', { vendor: '31"' }) } });
    expect(message.findings?.map((item) => `${item.id}:${item.vendor}`)).toEqual(['a:31"', 'b:25"']);
    message = applyChatEvent(message, { event: 'finding_dropped', data: { finding_id: 'a' } });
    expect(message.findings?.map((item) => item.id)).toEqual(['b']);
    message = applyChatEvent(message, { event: 'coverage', data: { coverage: { pages: [{ page: 1, status: 'same' }] } } });
    message = applyChatEvent(message, { event: 'trust', data: { trust: { levels: { confirmed: 1, likely: 0, to_check: 0 } } } });
    expect(message.coverage?.pages).toHaveLength(1);
    expect(message.trust?.levels?.confirmed).toBe(1);

    const ended = applyChatEvent(message, {
      event: 'message_end',
      data: {
        message: {
          id: 'm', role: 'assistant', created_at: '2026-10-17T10:00:00Z', text: 'One differs.',
          blocks: [
            { type: 'finding', finding: finding('c', { vendor: '1"' }) },
            { type: 'text', text: 'One differs.' },
            { type: 'finding', finding: finding('c', { vendor: '2"' }) },
          ],
        },
      },
    });
    expect(ended.findings?.map((item) => `${item.id}:${item.vendor}`)).toEqual(['c:2"']);
    // Findings are not drawn among the work: only the words stay in the blocks.
    expect(ended.blocks).toEqual([{ type: 'text', text: 'One differs.' }]);
    // The live coverage and trust stay when the stored message left them out.
    expect(ended.coverage?.pages).toHaveLength(1);
    expect(ended.trust?.levels?.confirmed).toBe(1);
  });

  it('reads saved finding, coverage and trust blocks', () => {
    const saved = fromSaved({
      id: 'm', role: 'assistant', created_at: '2026-10-17T10:00:00Z', text: '',
      blocks: [
        { type: 'finding', finding: finding('a') },
        { type: 'coverage', coverage: { pages: [{ page: 2, status: 'not_opened' }] } },
        { type: 'trust', trust: { gv_checks: { matched: 3, total: 4 } } },
      ],
    });
    expect(saved.findings).toHaveLength(1);
    expect(saved.coverage?.pages[0].status).toBe('not_opened');
    expect(saved.trust?.gv_checks?.matched).toBe(3);
  });
});

describe('coverage', () => {
  it('counts from the pages when the server sent no counts, one entry per page', () => {
    const coverage = {
      pages: [
        { page: 1, status: 'same' as const }, { page: 2, status: 'different' as const }, { page: 3, status: 'not_comparable' as const, note: 'Schedule only' },
        { page: 4, status: 'not_opened' as const }, { page: 4, status: 'same' as const },
      ],
    };
    const counts = coverageCounts(coverage);
    expect(counts).toEqual({ total: 4, same: 2, different: 1, not_comparable: 1, not_opened: 0 });
    expect(coverageWords(counts)).toBe('3 of 4 pages compared · 1 not comparable · 0 not opened');
    expect(coveragePageWords({ page: 3, pair_page: 30, status: 'not_comparable', note: 'Schedule only' })).toBe('Page 3 (vs 30): Not comparable. Schedule only');
  });

  it('prefers the server’s counts', () => {
    expect(coverageWords(coverageCounts({ pages: [], counts: { total: 60, same: 50, different: 8, not_comparable: 2, not_opened: 0 } })))
      .toBe('58 of 60 pages compared · 2 not comparable · 0 not opened');
  });
});

describe('short answers', () => {
  const base = { status: 'done' as const, stopped: null, files: [], findings: [], coverage: null, trust: null };
  it('a one- or two-line answer with no work is short', () => {
    expect(isShortAnswer({ ...base, blocks: [{ type: 'thinking', text: 'x', seconds: 2, live: false }, { type: 'text', text: 'Yes: 36".' }] })).toBe(true);
    expect(isShortAnswer({ ...base, blocks: [{ type: 'text', text: 'Line one.\nLine two.' }] })).toBe(true);
  });
  it('steps, findings, three lines or a table make it a full answer', () => {
    expect(isShortAnswer({ ...base, blocks: [{ type: 'step', step: { id: 's', tool: 'bash_tool', label: 'ls', ok: true }, live: false }, { type: 'text', text: 'Yes.' }] })).toBe(false);
    expect(isShortAnswer({ ...base, findings: [finding('a')], blocks: [{ type: 'text', text: 'Yes.' }] })).toBe(false);
    expect(isShortAnswer({ ...base, blocks: [{ type: 'text', text: 'a\nb\nc' }] })).toBe(false);
    expect(isShortAnswer({ ...base, blocks: [{ type: 'text', text: '| a |\n|---|' }] })).toBe(false);
    expect(isShortAnswer({ ...base, status: 'running', blocks: [{ type: 'text', text: 'Yes.' }] })).toBe(false);
  });
});

describe('trust', () => {
  const confirmed = finding('t1', {
    architect: '3\'-1"', vendor: '46"', inches: { architect: '37', vendor: '46', difference: '9' },
    trust_level: 'confirmed', checks: { printed_architect: 'match', printed_vendor: 'match', second_read: 'agrees' },
  });
  const disagreed = finding('t2', {
    verified: false, inches: { architect: '25.25', vendor: null, difference: null }, vendor: 'TBD',
    trust_level: 'to_check', checks: { printed_architect: 'match', printed_vendor: 'no_text', second_read: 'disagrees' },
  });

  it('shows code’s inches and difference, keeping the printed form', () => {
    expect(inchWords(25.25)).toBe('25 1/4');
    expect(inchWords(0.5)).toBe('1/2');
    expect(inchWords(-1.5)).toBe('−1 1/2');
    expect(inchWords(12.345)).toBe('12.345');
    expect(sideValue(confirmed, 'architect')).toEqual({ shown: '37 in', printed: '3\'-1"', notANumber: false });
    expect(changeWords(confirmed)).toBe('37 in → 46 in · +9 in');
    expect(sideValue(disagreed, 'vendor')).toEqual({ shown: 'TBD', printed: null, notANumber: true });
    // Without code's inches the printed words are shown as written, not marked.
    expect(sideValue(finding('p', { vendor: '30"' }), 'vendor')).toEqual({ shown: '30"', printed: null, notANumber: false });
  });

  it('says what passed and which check disagreed', () => {
    expect(findingChecks(confirmed).map((check) => `${check.key}:${check.state}`)).toEqual(['printed:pass', 'second_read:pass', 'verified:pass', 'rule:not_run']);
    const [printed, read, verified] = findingChecks(disagreed);
    expect(printed.state).toBe('unsure');
    expect(printed.words).toContain('vendor value drawn as pen strokes');
    expect(read).toMatchObject({ state: 'fail', words: 'Second read disagrees' });
    expect(verified).toMatchObject({ state: 'unsure', words: 'Helper lead, not checked' });
    const rule = findingChecks(finding('r', { source: 'vendor_arithmetic', parts: ['30', '6'], printed_total: '36', trust_level: 'confirmed', checks: { arithmetic: 'adds_up' } }))[3];
    expect(rule).toMatchObject({ name: 'Arithmetic', state: 'pass' });
    expect(rule.words).toBe('Code added up 30 + 6: matches the printed total 36');
  });

  it('counts levels and checks from the findings when the report has none', () => {
    expect(levelCounts([confirmed, disagreed], null)).toEqual({ confirmed: 1, likely: 0, to_check: 1 });
    expect(levelCounts([finding('n')], null)).toBeNull();
    expect(levelCounts([confirmed], { levels: { confirmed: 9, likely: 2, to_check: 1 } })).toEqual({ confirmed: 9, likely: 2, to_check: 1 });
    expect(checkLines(checkCounts([confirmed, disagreed], null))).toEqual([
      'Printed at the spot: 3 of 4 values found (1 drawn as pen strokes)',
      'Second independent read: 1 agree · 1 disagree · 0 unsure',
      'Checked on the drawing by the lead: 1 of 2',
      'GV rules applied by code: none for these findings',
    ]);
  });

  it('counts rounds', () => {
    const rounds = roundCounts([
      finding('a', { round_status: 'fixed_in_later_round' }), finding('b', { round_status: 'fixed_in_later_round' }), finding('c', { round_status: 'fixed_in_later_round' }),
      finding('d', { round_status: 'still_wrong' }), finding('e', { round_status: 'still_wrong' }), finding('f', { round_status: 'still_wrong' }),
      finding('g', { round_status: 'still_wrong' }), finding('h', { round_status: 'still_wrong' }), finding('i', { round_status: 'new_in_later_round' }),
    ], null);
    expect(rounds && roundWords(rounds)).toBe('Vendor fixed 3 of 8 round-1 items · 5 still wrong · 1 new');
    expect(roundCounts([finding('a', { round_status: 'single_round' })], null)).toBeNull();
    expect(roundWords({ fixed: 1, still_wrong: 0, new: 0, not_in_later: 1 })).toBe('Vendor fixed 1 of 2 round-1 items · 1 not in the later round');
  });

  it('says why a finding is AI reading only, from its checks', () => {
    expect(trustReason(disagreed)).toBe('Second read disagrees');
    expect(trustReason(confirmed)).toBeNull();
    const weak = finding('w', { verified: false, trust_level: 'to_check', checks: { printed_architect: 'no_box', printed_vendor: 'no_box', second_read: 'not_run', lead_checked: false } });
    expect(trustReason(weak)).toBe('0 of 4 checks passed');
    expect(findingChecks(weak)[0].state).toBe('not_run');
  });

  it('exports a CSV Excel opens, safe from formulas, with the trust columns', () => {
    expect(csvCell('=HYPERLINK("x")')).toBe('"\'=HYPERLINK(""x"")"');
    expect(csvCell('-1/2')).toBe('\'-1/2');
    expect(csvCell('-2')).toBe('-2');
    expect(csvCell(9)).toBe('9');
    const csv = findingsCsv([disagreed, confirmed]);
    expect(csv.charCodeAt(0)).toBe(0xfeff);
    const lines = csv.slice(1).trim().split('\r\n');
    expect(lines[0]).toContain('Trust,Evidence,Printed at spot,Second read');
    expect(lines).toHaveLength(3);
    expect(lines[1]).toContain('AI reading only');
    expect(lines[1]).toContain('Wrong');
    expect(lines[1]).toContain('disagrees');
    expect(lines[2]).toContain(',37,46,9,');
  });
});

describe('areas and one-line rows (#1508)', () => {
  it('reads the area an item names, or none', () => {
    expect(areaOf('Pantry (left) refrigerator outlet CL to right wall')).toBe('Pantry (left)');
    expect(areaOf('Nurse station overall width')).toBe('Nurse station');
    expect(areaOf('Storage Area-1 enclosure overall height')).toBe('Storage Area-1');
    expect(areaOf('Staff room cabinet overall width')).toBe('Staff room');
    expect(areaOf('Filler at wall end')).toBeNull();
    expect(areaOf('Material tags (counter / cabinets)')).toBeNull();
    expect(areaOf('Room elevation overall height')).toBeNull();
    expect(areaOf('Grab bar')).toBeNull();
    expect(areaOf('One two three four five overall width')).toBeNull();
  });

  it('groups by area (else the page’s area, else the page), the most wrong first, wrong rows first', () => {
    const items = [
      finding('a1', { page: 3, item: 'Coffee bar counter overall width', status: 'check' }),
      finding('a2', { page: 3, item: 'Filler at wall end', status: 'different' }),
      finding('b1', { page: 8, item: 'Nurse station overall width', status: 'different' }),
      finding('b2', { page: 9, vs_page: 8, item: 'Nurse station door count', status: 'different' }),
      finding('h1', { page: 12, item: 'Coffee point counter top tag', status: 'check' }),
      finding('h2', { page: 13, item: 'Coffee point station overall width', status: 'check' }),
      finding('m1', { page: 39, item: 'Storage overall height', status: 'check' }),
      finding('m2', { page: 40, item: 'Storage Area-1 enclosure overall height', status: 'check' }),
      finding('p1', { page: 15, item: 'Upper cabinets door layout', status: 'check' }),
      finding('s1', { page: 8, item: 'Nurse station drawer count', status: 'same' }),
    ];
    const groups = groupByArea(items, { status: 'all', category: null, page: null });
    expect(groups.map((group) => `${group.name}|${pagesWords(group.pages)}|${groupCountWords(group.counts)}|${group.findings.map((each) => each.id).join(',')}`)).toEqual([
      'Nurse station|p8–9|2 wrong|b1,b2',
      'Coffee bar|p3|1 wrong · 1 your decision|a2,a1',
      'Coffee point|p12–13|2 your decision|h1,h2',
      'Page 15|p15|1 your decision|p1',
      'Storage|p39|1 your decision|m1',
      'Storage Area-1|p40|1 your decision|m2',
    ]);
    expect(groups.find((group) => group.name === 'Page 15')?.byPage).toBe(true);
    // A filter never renames a group: the right item still joins its area.
    expect(groupByArea(items, { status: 'same', category: null, page: null }).map((group) => group.name)).toEqual(['Nurse station']);
    expect(pagesWords([3, 4, 8, 10, 11, 12])).toBe('p3–4, 8, 10–12');
    expect(pagesWords([])).toBe('');
  });

  it('says a row in one plain line, and a finding in the reviewer’s words (#1522)', () => {
    const wrong = finding('w', { item: 'Buffet lower outlet height', architect: '3\' - 1"', vendor: '46"', inches: { architect: '37', vendor: '46', difference: '9' } });
    expect(findingLine(wrong)).toEqual({ vendor: { text: '46 in', inches: true }, verb: 'needs', reference: { text: '37 in', inches: true }, printed: 'architect 3\' - 1"', difference: '+9 in' });
    const decide = finding('d', { status: 'check', item: 'Lower doors', architect: '3 lower doors with pulls; LA-002 tags', vendor: '4 drawers (round 2)', inches: null });
    expect(findingLine(decide)).toMatchObject({ vendor: { text: '4 drawers', inches: false }, verb: 'architect shows', reference: { text: '3 lower doors with pulls', inches: false }, difference: null });
    expect(findingLine({ ...decide, source: 'rules', architect: 'n/a' })).toMatchObject({ verb: 'GV’s rule says', reference: { text: 'nothing' } });
    const right = finding('r', { status: 'same', inches: { architect: '102', vendor: '102', difference: '0' } });
    expect(findingLine(right)).toMatchObject({ verb: 'both', vendor: { text: '102 in' } });
    expect(shortValue('n/a')).toBeNull();
    expect(shortValue('A very long sentence about the counter that goes on and on past the limit')).toMatch(/…$/);

    const words = reviewerWords({ ...wrong, why: '' });
    expect(words).toMatchObject({ referenceLabel: 'Architect shows', reference: '37 in (printed 3\' - 1")', vendor: '46 in (printed 46")', whyLabel: 'Why it’s wrong' });
    expect(words.why).toBe('The vendor’s 46 in is 9 in more than the architect’s 37 in.');
    expect(words.tell).toBe('Change it to 37 in to match the architect (now 46 in).');
    expect(reviewerWords(decide).whyLabel).toBe('What you need to decide');
    expect(reviewerWords(decide).tell).toBeNull();
    // Never internal names or "not a number", in any of the words.
    const all = JSON.stringify([words, reviewerWords(decide), reviewerWords({ ...decide, checks: { printed_architect: 'not_a_number', printed_vendor: 'no_text' } as ChatFinding['checks'] })]);
    expect(all).not.toMatch(/not a number|2nd read|Helper lead|To check/);
    expect(sureWords({ trust_level: 'to_check', checks: { printed_vendor: 'no_text' } as ChatFinding['checks'] })).toContain('pen strokes');
    expect(itemTitle('Buffet lower outlet height', 'Buffet')).toBe('Lower outlet height');
    // The spot: the box padded by 24 points; none when the picture's shape shows a cut crop.
    expect(spotInsets({ x0: 0, y0: 0, x1: 52, y1: 52 })).toEqual({ left: '24.00%', top: '24.00%', right: '24.00%', bottom: '24.00%' });
    expect(spotInsets({ x0: 0, y0: 0, x1: 52, y1: 52 }, { width: 300, height: 100 })).toBeNull();
    expect(spotInsets(null)).toBeNull();
  });

  it('tells a narrow answer from a whole-set one (#1522)', () => {
    const few = [finding('n1', { page: 8 }), finding('n2', { page: 8, vs_page: 9 })];
    expect(isNarrowAnswer({ findings: few, coverage: null }, false)).toBe(true);
    expect(isNarrowAnswer({ findings: few, coverage: null }, true)).toBe(false);
    expect(isNarrowAnswer({ findings: few, coverage: null, scope: { kind: 'whole_set' } }, false)).toBe(false);
    const many = [1, 2, 3, 4].map((page) => finding(`m${page}`, { page }));
    expect(isNarrowAnswer({ findings: many, coverage: null }, false)).toBe(false);
    expect(isNarrowAnswer({ findings: many, coverage: null, scope: { kind: 'pages', pages: [8], label: 'page 8' } }, false)).toBe(true);
    // The scope comes from the stream (#1528) and from a saved message's scope block.
    const live = applyChatEvent(localAnswer('k', { model: 'm', effort: 'high', thinking: true }), { event: 'scope', data: { index: null, scope: { kind: 'area', area: 'Buffet', label: 'the Buffet' } } });
    expect(live.scope).toEqual({ kind: 'area', area: 'Buffet', label: 'the Buffet' });
    const saved = fromSaved({ id: 's', role: 'assistant', created_at: '2026-10-17T10:00:00Z', text: 'x', blocks: [{ type: 'scope', scope: { kind: 'pages', pages: [8] } }] } as never);
    expect(saved.scope).toEqual({ kind: 'pages', pages: [8] });
  });
});
