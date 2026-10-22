// @vitest-environment jsdom
/**
 * #1862: the reading coverage under "How this was checked": numbers read of those printed (vendor
 * and architect), the main reasons, and the per-page table whose reasons open the page picture with
 * each unread number boxed. Synthetic data only.
 */
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { HowChecked } from '@/components/chat/chat-trust';
import type { ChatReading, ChatTrust } from '@/lib/chat-api';

const READING: ChatReading = {
  lines: [
    'Numbers read: 1,210 of about 1,400 printed on the vendor’s drawings (86%)',
    'Numbers read: 182 of about 221 printed on the architect’s drawings (82%)',
    'Not read on the vendor’s drawings, mostly: inside a note 73, stacked fraction 36, written sideways 26',
  ],
  vendor: { read: 1210, printed: 1400, percent: 86 },
  architect: { read: 182, printed: 221, percent: 82 },
  pages: [
    { file: 'set.pdf', page: 3, viewable: true, strokes_counted: true, vendor_read: 38, vendor_printed: 38, architect_read: 0, architect_printed: 0, not_read: [] },
    {
      file: 'set.pdf', page: 12, viewable: true, strokes_counted: true, vendor_read: 0, vendor_printed: 12, architect_read: 0, architect_printed: 0,
      not_read: [
        { reason: 'mm_row', words: 'mm over [inches] row', count: 2, spots: [{ side: 'vendor', box: [100, 200, 120, 214], kind: 'strokes' }, { side: 'vendor', box: [300, 200, 320, 214], kind: 'strokes' }] },
        { reason: 'gv_sticker', words: 'GV’s sticker or markup over it', count: 1, spots: [{ side: 'vendor', box: [400, 300, 420, 310], kind: 'strokes' }] },
      ],
    },
  ],
  pages_not_counted: 0,
};

function show(trust: ChatTrust, pagePicture: ((page: number) => string) | null = (page) => `/pages/${page}.png`) {
  render(<HowChecked findings={[]} trust={trust} coverage={null} pagePicture={pagePicture} />);
  return screen.getByRole('region', { name: 'How this was checked' });
}

describe('the reading coverage in “How this was checked”', () => {
  it('says numbers read of about how many printed, for the vendor and the architect, and why the rest were not', async () => {
    const user = userEvent.setup();
    const node = show({ reading: READING });
    await user.click(within(node).getByRole('button', { name: 'How this was checked' }));
    const section = node.querySelector('[data-slot="chat-reading"]') as HTMLElement;
    expect(section).not.toBeNull();
    const sides = section.querySelectorAll('[data-slot="chat-reading-side"]');
    expect([...sides].map((side) => side.textContent)).toEqual([
      'Vendor’s drawings1,210 of about 1,400 read · 86%',
      'Architect’s drawings182 of about 221 read · 82%',
    ]);
    expect(within(section).getByText(/mostly: inside a note 73/)).toBeTruthy();
  });

  it('lists every page; a reason opens the page picture with each unread number boxed, one at a time', async () => {
    const user = userEvent.setup();
    const node = show({ reading: READING });
    await user.click(within(node).getByRole('button', { name: 'How this was checked' }));
    await user.click(within(node).getByRole('button', { name: /By page: 1 of 2 pages have numbers not read/ }));
    const rows = node.querySelectorAll('[data-slot="chat-reading-page"]');
    expect(rows).toHaveLength(2);
    expect(rows[0].textContent).toContain('All read');
    const mm = within(rows[1] as HTMLElement).getByRole('button', { name: 'mm over [inches] row, 2: show on the page' });
    await user.click(mm);
    expect(mm.getAttribute('aria-pressed')).toBe('true');
    const spot = (rows[1] as HTMLElement).querySelector('[data-slot="chat-reading-spot"]') as HTMLElement;
    expect(within(spot).getByRole('img', { name: 'Page 12' }).getAttribute('src')).toBe('/pages/12.png');
    expect(within(spot).getByText('1 of 2')).toBeTruthy();
    await user.click(within(spot).getByRole('button', { name: 'Next number' }));
    expect(within(spot).getByText('2 of 2')).toBeTruthy();
  });

  it('without the page picture the reasons are plain words, and an answer with no coverage shows none', async () => {
    const user = userEvent.setup();
    const node = show({ reading: READING }, null);
    await user.click(within(node).getByRole('button', { name: 'How this was checked' }));
    await user.click(within(node).getByRole('button', { name: /By page/ }));
    expect(within(node).queryByRole('button', { name: /mm over \[inches\] row/ })).toBeNull();
    expect(within(node).getByText('mm over [inches] row')).toBeTruthy();
  });

  it('shows nothing for an answer made before coverage was counted', async () => {
    const user = userEvent.setup();
    const node = show({ reading: null });
    await user.click(within(node).getByRole('button', { name: 'How this was checked' }));
    expect(node.querySelector('[data-slot="chat-reading"]')).toBeNull();
  });
});
