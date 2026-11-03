// @vitest-environment jsdom
/**
 * The reading ledger and the revision diff on the screen (#1496): the trust card's ledger line, the
 * server's GV-checks shape, the "Reference view" / "This view" labels of a cross-view finding, the
 * sum written out for a vendor-arithmetic finding, and the rounds line from compare_rounds.
 * Synthetic data only.
 */
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { AssistantMessage } from '@/components/chat/chat-message';
import type { ChatTrust, LedgerReport } from '@/lib/chat-api';
import type { ViewMessage } from '@/lib/chat-messages';
import { CSV_HEADER, findingsCsv, gvChecksOf, ledgerWords, roundWords, sumWords } from '@/lib/chat-trust';
import { referenceValueLabel, referenceValueWord, sideLabels } from '@/lib/chat-findings';
import { referenceSide } from '@/lib/chat-drawer';
import { finding, OPUS } from './fixtures/chat';

function answer(change: Partial<ViewMessage> = {}): ViewMessage {
  return {
    key: 'a', id: 'msg-1', role: 'assistant', created_at: '2026-10-17T10:00:00Z', text: '', model: 'claude-opus-5-5', effort: 'high', thinking: true,
    blocks: [{ type: 'text', text: 'Checked.' }], files: [], tasks: null, cost_usd: '1.20', stopped: null, error: null, status: 'done', ...change,
  };
}

function show(message: ViewMessage) {
  render(<AssistantMessage message={message} chatId="c-1" models={[OPUS]} messageCapUsd="5.00" />);
  return screen.getByRole('article', { name: 'Answer' });
}

const LEDGER: LedgerReport = {
  strings: { total: 41, add_up: 38, off: 3, not_a_number: 0, vendor_off: 3, architect_off: 0 },
  views: { groups: 14, consistent: 10, inconsistent: 2, not_compared: 2, typical_groups: 1 },
  completeness: { compared: true, architect_items: 31, vendor_items: 30, missing_on_vendor: ['W30 wall'], extra_on_vendor: [], not_looked_at: [] },
};

const CROSS = finding('x1', {
  item: 'B36 base width (same item, different views)', source: 'cross_view', reference_side: 'vendor', status: 'check',
  architect: '36"', vendor: '35"', created_by: 'ledger', ledger_ids: ['R2', 'R4'],
});
const SUM = finding('s1', {
  item: 'Dimension string (plan)', source: 'vendor_arithmetic', architect: '27 1/2"', vendor: '27 1/4"', parts: ['12"', '15 1/2"'], printed_total: '27 1/4"',
  sum_text: '12" + 15 1/2" = 27 1/2" ≠ 27 1/4" printed (off 1/4")', created_by: 'ledger', checks: { arithmetic: 'does_not_add_up' },
});

describe('the reading ledger in words', () => {
  it('says the ledger line the trust card shows, all code’s counts', () => {
    expect(ledgerWords(LEDGER)).toBe('41 strings · 38 add up · 3 don’t · 12 items checked across views (2 disagree) · 1 missing on vendor');
    expect(ledgerWords(null)).toBeNull();
    expect(ledgerWords({ ...LEDGER, views: { ...LEDGER.views, inconsistent: 0, consistent: 12 } })).toContain('12 items checked across views · 1 missing');
  });

  it('writes a vendor string’s sum out: code’s words, else the parts as printed (never added up here)', () => {
    expect(sumWords(SUM)).toBe('12" + 15 1/2" = 27 1/2" ≠ 27 1/4" printed (off 1/4")');
    expect(sumWords({ ...SUM, sum_text: null })).toBe('12" + 15 1/2" vs 27 1/4" printed');
    expect(sumWords(CROSS)).toBeNull();
  });

  it('names a cross-view finding’s columns by view', () => {
    expect(sideLabels(CROSS)).toMatchObject({ architect: 'Reference view', vendor: 'This view' });
    // #1581: the vendor's own string is the vendor's on both sides, never "Architect".
    expect(sideLabels(SUM)).toEqual({ architect: 'Vendor: the parts', vendor: 'Vendor: the printed total', architectTag: 'V', vendorTag: 'V' });
    expect(sideLabels({ source: 'cross_view', reference_side: 'vendor', created_by: 'consistency' })).toMatchObject({ architect: 'Vendor: the same span, elsewhere', architectTag: 'V' });
    expect(sideLabels({ source: 'architect' })).toEqual({ architect: 'Architect', vendor: 'Vendor', architectTag: 'A', vendorTag: 'V' });
  });

  it('names another vendor sheet’s value by its page, never as the architect’s (2026-11-04)', () => {
    const other = { source: 'cross_view' as const, reference_side: 'vendor' as const, created_by: 'sink_sheets', page: 17, arch_box: { page: 12, x0: 1, y0: 1, x1: 9, y1: 9 } };
    expect(referenceValueLabel(other)).toBe('Vendor p.12');
    expect(referenceValueWord(other)).toBe('vendor p.12');
    // The same page (two views of one sheet) keeps its view words; an architect item stays the architect's.
    expect(referenceValueLabel({ ...other, arch_box: { ...other.arch_box, page: 17 } })).toBe('Reference view');
    expect(referenceValueWord({ source: 'architect' })).toBe('architect');
  });

  it('names a vendor string’s sum as its parts adding up, never as an architect value (Set A, 2026-11-04)', () => {
    expect(referenceValueLabel(SUM)).toBe('Parts add up to');
    expect(referenceValueLabel({ source: 'architect' })).toBe('Architect');
    expect(referenceValueWord(SUM)).toBe('parts add up to');
    expect(referenceValueWord({ source: 'architect' })).toBe('architect');
    expect(referenceSide(SUM, 'likely_problem')).toMatchObject({ label: 'Parts add up to', value: '27 1/2″' });
    const csv = findingsCsv([{ ...SUM, inches: { architect: '27.5', vendor: '27.25', difference: '-0.25' } }]);
    const row = csv.replace(/^\uFEFF/, '').split('\r\n')[1] ?? csv.split('\n')[1];
    expect(row).toContain('parts add up to 27 1/2');
    expect(row).not.toContain('27.5');
    expect(CSV_HEADER).toContain('Architect (as printed)');
  });

  it('reads GV Review’s own checks in the server’s shape, with the ledger’s extra left-out ones', () => {
    const trust: ChatTrust = {
      gv_checks: { recorded: 9, included: 8, left_out: [{ record_id: 'c4', page: 12, what: 'Upper cabinet height' }] },
      recorded_results: { recorded: 10, included: 8, missing: [], also_left_out: [{ key: 'rl7', pages: [2], what: 'Filler width' }] },
    };
    const gv = gvChecksOf(trust)!;
    expect([gv.matched, gv.total]).toEqual([8, 10]);
    expect(gv.leftOut.map((item) => item.item)).toEqual(['Upper cabinet height', 'Filler width']);
  });

  it('uses the rounds line code wrote for compare_rounds', () => {
    const line = 'Round 1 comments: 1 of 3 closed · 2 previous comments not addressed · 2 changes nobody asked for';
    expect(roundWords({ fixed: 1, still_wrong: 2, new: 0, unrequested: 2, line })).toBe(line);
    // Without code's line, the screen says it in the comment letter's round words (#1712).
    expect(roundWords({ fixed: 1, still_wrong: 2, new: 0, unrequested: 2 })).toBe('Closed 1 of 3 round-1 items · 2 previous comments not addressed · 2 changes nobody asked for');
  });
});

describe('the reading ledger on the screen', () => {
  it('shows the ledger in “How this was checked”, the items it names behind a disclosure', async () => {
    const user = userEvent.setup();
    const trust: ChatTrust = {
      ledger: LEDGER,
      gv_checks: { recorded: 9, included: 9, left_out: [] },
      rounds: { fixed: 1, still_wrong: 2, new: 0, unrequested: 2, line: 'Round 1 comments: 1 of 3 closed · 2 previous comments not addressed · 2 changes nobody asked for' },
    };
    const node = show(answer({ findings: [CROSS, SUM], trust, scope: { kind: 'whole_set' } }));
    const summary = within(node).getByRole('region', { name: 'How this was checked' });
    await user.click(within(summary).getByRole('button', { name: 'How this was checked' }));
    const ledger = within(summary).getByRole('button', { name: /Read and checked by code: 41 strings · 38 add up · 3 don’t/ });
    expect(ledger.textContent).toContain('1 missing on vendor');
    await user.click(ledger);
    expect(within(summary).getByText('W30 wall')).toBeTruthy();
    expect(within(summary).getByText('Matches GV Review’s own checks:', { exact: false }).textContent).toContain('9 of 9');
    expect(within(summary).getByText('Round 1 comments: 1 of 3 closed · 2 previous comments not addressed · 2 changes nobody asked for')).toBeTruthy();
  });

  it('says a cross-view item by its views, and code’s sum in a vendor string’s card', () => {
    // A narrow answer (one page, two items): both cards are open at first.
    const node = show(answer({ findings: [CROSS, SUM] }));
    const cross = node.querySelector<HTMLElement>('[data-slot="chat-finding-row"][data-id="x1"]')!;
    expect(cross.querySelector('[data-slot="chat-finding-source"]')?.textContent).toBe('same item, another view');
    const detail = node.querySelector<HTMLElement>('[data-slot="chat-finding-detail"][data-id="x1"]')!;
    expect([...detail.querySelectorAll('dt')].map((term) => term.textContent).slice(0, 2)).toEqual(['Reference view shows', 'This view shows']);
    const sum = node.querySelector<HTMLElement>('[data-slot="chat-finding-detail"][data-id="s1"]')!;
    expect(sum.querySelector('[data-slot="chat-finding-sum"]')?.textContent).toBe('12" + 15 1/2" = 27 1/2" ≠ 27 1/4" printed (off 1/4")');
  });

  it('names the pictures by view, in the card and in the evidence viewer', async () => {
    const user = userEvent.setup();
    const node = show(answer({ findings: [CROSS] }));
    const card = node.querySelector<HTMLElement>('[data-slot="chat-finding-detail"][data-id="x1"]')!;
    expect([...card.querySelectorAll('figcaption span:first-child')].map((label) => label.textContent)).toEqual(['Reference view', 'This view']);
    await user.click(within(node).getByRole('button', { name: 'Open viewer' }));
    const dialog = screen.getByRole('dialog');
    // #1717: the panel shows the vendor's side first (its value, then what it is compared with).
    expect([...dialog.querySelectorAll('[data-slot="chat-evidence-side"] figcaption span:first-child')].map((label) => label.textContent)).toEqual(['This view', 'Reference view']);
  });

  it('shows the sum written out in the evidence viewer', async () => {
    const user = userEvent.setup();
    const node = show(answer({ findings: [SUM] }));
    await user.click(within(node).getByRole('button', { name: 'Open viewer' }));
    expect(screen.getByRole('dialog').querySelector('[data-slot="chat-evidence-sum"]')?.textContent).toBe('Code’s sum: 12" + 15 1/2" = 27 1/2" ≠ 27 1/4" printed (off 1/4")');
  });
});
