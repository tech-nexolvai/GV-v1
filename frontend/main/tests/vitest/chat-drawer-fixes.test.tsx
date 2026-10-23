// @vitest-environment jsdom
/**
 * The result drawer after the client meeting (2026-10-23), on top of results v2: a vendor string
 * code did not compare shows ONE picture of its row and plain words (never the AI's unproven numbers
 * as "vendor vs …"); the architect's side has its own "Whole page"; an architect page with no crop
 * shows that page (or says plainly why there is no picture); a long value is said short in the
 * header with the whole of it below. Made-up values only.
 */
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { AssistantMessage } from '@/components/chat/chat-message';
import type { ChatCoverage, ChatFinding, PageWhere } from '@/lib/chat-api';
import { chatPaths } from '@/lib/chat-api';
import { howWeKnow, noArchitectPictureWords, overallSpot, REFUSED_STRING_WORDS, refusedStringSteps, ringInCrop } from '@/lib/chat-drawer';
import { isRefusedString, shortValue } from '@/lib/chat-result';
import type { ViewMessage } from '@/lib/chat-messages';
import { finding, OPUS } from './fixtures/chat';

function answer(findings: ChatFinding[]): ViewMessage {
  return {
    key: 'a', id: 'msg-1', role: 'assistant', created_at: '2026-10-23T10:00:00Z', text: '', model: 'claude-opus-5-5', effort: 'high', thinking: true,
    blocks: [{ type: 'text', text: 'Done.' }], files: [], tasks: null, cost_usd: '4.10', stopped: null, error: null, status: 'done', findings, coverage: COVERAGE,
  };
}

const COVERAGE: ChatCoverage = {
  pages: [{ page: 3, status: 'different' }, { page: 5, status: 'same' }, { page: 6, status: 'same' }],
  counts: { total: 3, same: 2, different: 1, not_comparable: 0, not_opened: 0 },
};

const GAP = 'code could not find the overall’s whole dimension line (it is not drawn to its value at the drawing’s scale), so the whole run cannot be seen';
const ROW = { page: 3, x0: 100, y0: 200, x1: 140, y1: 440, file: 'vendor.pdf' };

/** A vendor string code refused: the parts and the overall are the AI's reading, not proved. */
const REFUSED = finding('s1', {
  item: 'Sample unit A: dimension string (up)', source: 'vendor_arithmetic', created_by: 'ledger', arch_picture_id: null, status: 'different',
  vendor: '18"', architect: '20 3/8"', parts: ['2 3/8"', '14"', '4"'], parts_sum: '20.375', printed_total: '18"',
  sum_text: '2 3/8" + 14" + 4" = 20 3/8" ≠ 18" printed (off 2 3/8")', vendor_box: ROW,
  dims: { vendor: { id: 'D7', page: 3, file: 'vendor.pdf', text: '18"', inches: '18', box: [104, 300, 124, 312] } },
  proof: { group: 'not_compared', not_compared: 'string_not_verified', reason: GAP, settle: 'code’s check that the parts run end to end between the overall’s extension lines' },
});

const ARCH_BOX = { page: 6, x0: 300, y0: 200, x1: 340, y1: 214, file: 'Made-up architect set.pdf' };

/** An item that names its architect page, with no crop of it. */
const NO_CROP = finding('n1', {
  page: 5, item: 'Sample unit B: countertop material', status: 'check', vendor: 'QZ-2 quartz, 1 1/2" edge', architect: 'QZ-2 quartz top and splash (section note)',
  vs_page: 6, arch_box: ARCH_BOX, arch_picture_id: null, vendor_box: { page: 5, x0: 10, y0: 10, x1: 40, y1: 20, file: 'vendor.pdf' },
  proof: { group: 'not_compared', not_compared: 'nothing_to_compare', reason: 'architect gives no size or information for it', settle: 'the architect’s tag' },
});

/** A long architect value and no architect page at all. */
const LONG = finding('n2', {
  page: 5, item: 'Sample unit C: overall length (rear view)', status: 'check', vendor: '187 1/2"', architect: 'no overall printed (partial dims 1\'-5", 7\'-2", 1\'-9")',
  arch_picture_id: null, vendor_box: { page: 5, x0: 50, y0: 50, x1: 90, y1: 62, file: 'vendor.pdf' },
  proof: { group: 'not_compared', not_compared: 'nothing_to_compare', reason: 'architect gives no size or information for it', settle: 'the architect’s overall' },
});

/** A proven Wrong against the architect, with both crops. */
const WRONG = finding('w1', {
  page: 5, item: 'Sample unit D: sink base width', vendor: '46"', architect: '37"', vs_page: 6, arch_box: ARCH_BOX,
  vendor_box: { page: 5, x0: 10, y0: 10, x1: 40, y1: 20, file: 'vendor.pdf' }, proof: { group: 'clouded', should_be: '37" (architect, page 6)' },
});

const pagePicture = (page: number, where?: PageWhere) => chatPaths.planPage('p', page, where);

describe('the drawer fixes, without React', () => {
  it('knows a refused vendor string from a proven one', () => {
    expect(isRefusedString(REFUSED, 'not_compared')).toBe(true);
    expect(isRefusedString(REFUSED, 'clouded')).toBe(false);
    expect(isRefusedString({ ...REFUSED, arch_picture_id: 'a-1' }, 'not_compared')).toBe(false);
    expect(isRefusedString(LONG, 'not_compared')).toBe(false);
  });

  it('says a refused string plainly, without the unproven numbers', () => {
    const steps = howWeKnow(REFUSED, 'not_compared');
    expect(steps[0]).toBe(REFUSED_STRING_WORDS);
    expect(steps[1]).toMatch(/^The app could not find the overall’s whole dimension line/);
    expect(steps.join(' ')).not.toMatch(/20 3\/8|2 3\/8|≠/);
    // Code's reason that opens with the reading itself loses it (and says nothing twice).
    const read = refusedStringSteps({ proof: { group: 'not_compared', not_compared: 'string_not_verified', reason: 'parts 2 3/8" + 14" + 4" = 20 3/8"; printed 18"; code could not verify on the drawing that these parts are one string' } });
    expect(read).toEqual([REFUSED_STRING_WORDS, 'Nothing is asked of the vendor for it. If the row looks wrong to you, you can add a red mark yourself.']);
  });

  it('rings the printed overall inside its row', () => {
    const overall = overallSpot(REFUSED);
    expect(overall).toEqual({ page: 3, x0: 104, y0: 300, x1: 124, y1: 312, file: 'vendor.pdf' });
    // The crop is the row padded 24 points: 88 x 288 points.
    const ring = ringInCrop(ROW, overall, 880, 2880)!;
    expect(ring.left).toBeCloseTo(28 / 88);
    expect(ring.top).toBeCloseTo(124 / 288);
    expect(ring.width).toBeCloseTo(20 / 88);
    expect(ringInCrop(ROW, { x0: 400, y0: 300, x1: 420, y1: 310 }, 880, 2880)).toBeNull();
    expect(overallSpot({ ...REFUSED, dims: null })).toBeNull();
  });

  it('says a long value short, with the whole of it apart', () => {
    expect(shortValue('no overall printed (partial dims 1\'-5", 7\'-2", 1\'-9")')).toEqual({ short: 'No overall printed', full: 'no overall printed (partial dims 1′-5″, 7′-2″, 1′-9″)' });
    expect(shortValue('187 1/2"')).toEqual({ short: '187 1/2″', full: null });
    expect(shortValue('QZ-2 quartz top and splash (section note)').short).toBe('QZ-2 quartz top…');
    expect(shortValue('the architect\'s note on the left side of it').full).toContain('architect\'s');
  });

  it('says why the architect side has no picture', () => {
    expect(noArchitectPictureWords(LONG, false)).toBe('The architect’s drawings give no size or note for this item, so there is no spot to show');
    expect(noArchitectPictureWords(LONG, true)).toBe('No architect drawing of this item in the upload');
    expect(noArchitectPictureWords({ ...LONG, proof: null }, false)).toBe('The app did not find this item’s spot on the architect’s drawings, so there is no picture of it');
  });

  it('asks for another upload’s page by name, or the architect’s file', () => {
    expect(chatPaths.planPage('p', 6, { file: 'Made-up architect set.pdf' })).toBe('/api/v1/packages/p/plan-pages/6?file=Made-up%20architect%20set.pdf');
    expect(chatPaths.planPage('p', 6, { side: 'architect' })).toBe('/api/v1/packages/p/plan-pages/6?side=architect');
    expect(chatPaths.planPage('p', 6)).toBe('/api/v1/packages/p/plan-pages/6');
  });
});

async function openRow(id: string, findings: ChatFinding[], tab: RegExp = /Not compared/) {
  const user = userEvent.setup();
  render(<AssistantMessage message={answer(findings)} chatId="c-1" models={[OPUS]} fullCheck pagePicture={pagePicture} />);
  const node = screen.getByRole('article', { name: 'Answer' });
  await user.click(within(node).getByRole('tab', { name: tab }));
  const row = node.querySelector<HTMLElement>(`[data-slot="chat-result-row"][data-id="${id}"]`)!;
  await user.click(row);
  return { user, node, row, panel: await screen.findByRole('dialog') };
}

describe('the drawer fixes on the screen', () => {
  it('a refused string: one picture of its row, plain words, no "vendor vs" values', async () => {
    const { panel, row } = await openRow('s1', [REFUSED, NO_CROP]);
    // The list row shows no numbers for it either.
    expect(row.textContent).not.toMatch(/18″|20 3\/8/);
    const sides = panel.querySelectorAll('[data-slot="chat-evidence-side"]');
    expect(sides).toHaveLength(1);
    expect(sides[0].querySelector('figcaption')?.textContent).toBe('The row on the vendor’s drawing');
    expect(sides[0].querySelector('img')?.getAttribute('src')).toBe('/api/v1/chats/c-1/pictures/vend-s1');
    expect(panel.textContent).not.toMatch(/Vendor shows|Vendor’s own drawing|20 3\/8/);
    expect(panel.querySelector('[data-slot="chat-evidence-legend"]')?.textContent).toContain('dashed box: its overall');
    const how = within(panel).getByRole('region', { name: 'How we know' });
    expect(within(how).getAllByRole('listitem')[0].textContent).toBe(REFUSED_STRING_WORDS);
  });

  it('an architect page with no crop shows that page, ringed, with its own Whole page', async () => {
    const { user, panel } = await openRow('n1', [REFUSED, NO_CROP]);
    const architect = panel.querySelector<HTMLElement>('[data-slot="chat-evidence-side"][data-side="architect"]')!;
    expect(architect.textContent).not.toContain('No picture');
    expect(architect.querySelector('img')?.getAttribute('src')).toBe('/api/v1/packages/p/plan-pages/6?file=Made-up%20architect%20set.pdf');
    expect(architect.textContent).toContain('Architect page 6');
    await user.click(within(architect).getByRole('button', { name: 'Whole page' }));
    expect(within(architect).getByRole('button', { name: 'Back to the spot' }).getAttribute('aria-pressed')).toBe('true');
    // The long words are said short in the header, whole below.
    expect(architect.querySelector('[data-slot="chat-evidence-value"]')?.textContent).toBe('QZ-2 quartz top…');
    expect(architect.querySelector('[data-slot="chat-evidence-value-full"]')?.textContent).toBe('In full: QZ-2 quartz top and splash (section note)');
  });

  it('a long architect value with no architect page: short in the header, whole below, and why there is no picture', async () => {
    const { panel } = await openRow('n2', [LONG]);
    const architect = panel.querySelector<HTMLElement>('[data-slot="chat-evidence-side"][data-side="architect"]')!;
    expect(architect.querySelector('[data-slot="chat-evidence-value"]')?.textContent).toBe('No overall printed');
    expect(architect.querySelector('[data-slot="chat-evidence-value-full"]')?.textContent).toBe('In full: no overall printed (partial dims 1′-5″, 7′-2″, 1′-9″)');
    expect(architect.querySelector('[data-slot="chat-evidence-missing"]')?.textContent).toBe('The architect’s drawings give no size or note for this item, so there is no spot to show');
    expect(within(architect).queryByRole('button', { name: 'Whole page' })).toBeNull();
  });

  it('a proven Wrong keeps its two crops; the architect side gets Whole page of the architect’s file', async () => {
    const { user, panel } = await openRow('w1', [WRONG], /Wrong/);
    const sides = [...panel.querySelectorAll<HTMLElement>('[data-slot="chat-evidence-side"]')];
    expect(sides.map((side) => side.querySelector('figcaption')?.textContent)).toEqual(['Vendor shows46″', 'Should be37″']);
    expect(sides[1].querySelector('img')?.getAttribute('src')).toBe('/api/v1/chats/c-1/pictures/arch-w1');
    await user.click(within(sides[1]).getByRole('button', { name: 'Whole page' }));
    expect(sides[1].querySelector('img')?.getAttribute('src')).toBe('/api/v1/packages/p/plan-pages/6?file=Made-up%20architect%20set.pdf');
    // The vendor side's own Whole page is still the vendor's page.
    await user.click(within(sides[0]).getByRole('button', { name: 'Whole page' }));
    expect(sides[0].querySelector('img')?.getAttribute('src')).toBe('/api/v1/packages/p/plan-pages/5');
  });
});
