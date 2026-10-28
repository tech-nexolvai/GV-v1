// @vitest-environment jsdom
/**
 * The evidence panel's header says the item's FINAL group (2026-10-22): a Wrong code did not prove,
 * or an item another one carries ("Unchanged since round 1"), is Not compared there too, never
 * "Wrong". Synthetic data only.
 */
import { render } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { EvidenceViewer } from '@/components/chat/chat-evidence';
import { finding } from './fixtures/chat';

describe('the evidence panel header', () => {
  it('shows Not compared for a carried, unproved Wrong, as the list and the counts do', () => {
    const carried = finding('a', {
      item: 'Lounge cabinet: overall height to the bulkhead', status: 'different', round_status: 'not_in_later_round',
      round_match: { page: 7, round: 1, vendor_round: 2, value: '45 11/16"', unchanged: true, compared: 'carrier', words: 'Unchanged since round 1' },
      proof: { group: 'not_compared', not_compared: 'different_reference', reason: 'not proved' },
    });
    render(<EvidenceViewer findings={[carried]} openId="a" onOpen={vi.fn()} pictureUrl={(id) => `/p/${id}`} />);
    const header = document.querySelector('[data-slot="chat-evidence-viewer"] [data-slot="chat-finding-status"]') as HTMLElement;
    expect(header.getAttribute('data-status')).toBe('not_compared');
    expect(header.textContent).toBe('Not compared');
    expect(document.querySelector('[data-slot="chat-round-match"]')?.textContent).toBe('Unchanged since round 1');
  });

  it('still shows Wrong for a proved red', () => {
    const red = finding('b', { status: 'different', proof: { group: 'clouded' } });
    render(<EvidenceViewer findings={[red]} openId="b" onOpen={vi.fn()} pictureUrl={(id) => `/p/${id}`} />);
    const header = document.querySelector('[data-slot="chat-evidence-viewer"] [data-slot="chat-finding-status"]') as HTMLElement;
    expect(header.getAttribute('data-status')).toBe('different');
    expect(header.textContent).toBe('Wrong');
  });
});
