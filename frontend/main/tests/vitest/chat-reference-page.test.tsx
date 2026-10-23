// @vitest-environment jsdom
/**
 * One page of truth per card (stream V, 2026-10-23): with two separate uploads, every architect
 * picture, caption and page number of a finding reads its ONE reference page, set by code from its
 * vendor page's pair. Never page N of the first architect file, never the vendor's page number under
 * an architect label, never a guessed page. Made-up file names and values only.
 */
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { AssistantMessage } from '@/components/chat/chat-message';
import type { ChatCoverage, ChatFinding, PageWhere } from '@/lib/chat-api';
import { chatPaths } from '@/lib/chat-api';
import { architectPage, pageWords, referenceWhere } from '@/lib/chat-findings';
import type { ViewMessage } from '@/lib/chat-messages';
import { finding, OPUS } from './fixtures/chat';

const ARCH_B = 'Made-up architect hotel B.pdf';
const VENDOR_BOX = { page: 3, x0: 10, y0: 10, x1: 40, y1: 20, file: 'vendor.pdf' };

/** No architect spot; its pair is page 7 of the SECOND architect file. */
const PAIRED = finding('p1', {
  page: 3, vs_page: 7, item: 'Sample unit E: top material', status: 'check', vendor: 'QZ-2', architect: 'QZ-2 (note)', arch_box: null, arch_picture_id: null,
  vendor_box: VENDOR_BOX, reference_page: { file: ARCH_B, page: 7, why: null },
  proof: { group: 'not_compared', not_compared: 'nothing_to_compare', reason: 'architect gives no size or information for it', settle: 'the architect’s tag' },
});

/** A vendor page no architect page is paired with. */
const UNPAIRED = finding('u1', {
  page: 2, item: 'Sample unit F: sink model', status: 'check', vendor: 'K-1', architect: 'not compared', arch_box: null, arch_picture_id: null,
  vendor_box: { ...VENDOR_BOX, page: 2 }, reference_page: { file: null, page: null, why: 'No architect page is paired with vendor page 2 (no clear match).' },
  proof: { group: 'not_compared', not_compared: 'nothing_to_compare', reason: 'architect gives no size or information for it', settle: 'the architect’s drawing' },
});

/** Vendor-only because of its parts; its vs_page is an architect page number (20). */
const PARTS = finding('v1', {
  page: 3, vs_page: 20, item: 'Sample unit G: dimension string', status: 'check', source: 'architect', parts: ['10"', '28"'], vendor: '38"', architect: '38"',
  arch_box: null, arch_picture_id: null, vendor_picture_id: null, vendor_box: VENDOR_BOX,
  proof: { group: 'not_compared', not_compared: 'nothing_to_compare', reason: 'architect gives no size or information for it', settle: 'the architect’s overall' },
});

const COVERAGE: ChatCoverage = {
  pages: [{ page: 2, status: 'not_comparable' }, { page: 3, status: 'different' }],
  counts: { total: 2, same: 0, different: 1, not_comparable: 1, not_opened: 0 },
};

function answer(findings: ChatFinding[]): ViewMessage {
  return {
    key: 'a', id: 'msg-1', role: 'assistant', created_at: '2026-10-23T10:00:00Z', text: '', model: 'claude-opus-5-5', effort: 'high', thinking: true,
    blocks: [{ type: 'text', text: 'Done.' }], files: [], tasks: null, cost_usd: '1.00', stopped: null, error: null, status: 'done', findings, coverage: COVERAGE,
  };
}

const asked: string[] = [];
const pagePicture = (page: number, where?: PageWhere) => {
  const url = chatPaths.planPage('p', page, where);
  asked.push(url);
  return url;
};

async function openRow(id: string, findings: ChatFinding[]) {
  asked.length = 0;
  const user = userEvent.setup();
  render(<AssistantMessage message={answer(findings)} chatId="c-1" models={[OPUS]} fullCheck pagePicture={pagePicture} />);
  const node = screen.getByRole('article', { name: 'Answer' });
  await user.click(within(node).getByRole('tab', { name: /Not compared/ }));
  await user.click(node.querySelector<HTMLElement>(`[data-slot="chat-result-row"][data-id="${id}"]`)!);
  const panel = await screen.findByRole('dialog');
  return { panel, architect: panel.querySelector<HTMLElement>('[data-slot="chat-evidence-side"][data-side="architect"]') };
}

describe('the reference page, without React', () => {
  it('reads the stamped page; none means none, never the vendor’s page number', () => {
    expect(referenceWhere(PAIRED)).toEqual({ file: ARCH_B, page: 7 });
    expect(architectPage(UNPAIRED)).toBeNull();
    expect(pageWords(UNPAIRED)).toBe('2');
    expect(pageWords(PAIRED)).toBe('3 vs 7');
    // The spot's page wins (its picture shows it); an older answer keeps vs_page, then its page.
    expect(referenceWhere({ ...PAIRED, arch_box: { page: 7, x0: 1, y0: 1, x1: 2, y1: 2, file: ARCH_B } })).toEqual({ file: ARCH_B, page: 7 });
    expect(architectPage({ page: 4, vs_page: null, arch_box: null })).toBe(4);
  });
});

describe('the drawer reads the reference page', () => {
  it('two architect files: the pair’s page of ITS file, never page N of the first one', async () => {
    const { architect } = await openRow('p1', [PAIRED, UNPAIRED]);
    expect(architect!.querySelector('img')?.getAttribute('src')).toBe(`/api/v1/packages/p/plan-pages/7?file=${encodeURIComponent(ARCH_B)}`);
    expect(asked.some((url) => url.includes('side=architect'))).toBe(false);
    expect(architect!.textContent).toContain('Architect page 7 (Made-up architect hotel B)');
  });

  it('an unpaired page: no architect picture, with the true reason', async () => {
    const { architect } = await openRow('u1', [PAIRED, UNPAIRED]);
    expect(architect!.querySelector('img')).toBeNull();
    expect(architect!.querySelector('[data-slot="chat-evidence-missing"]')?.textContent).toBe('No architect picture: no architect page is paired with vendor page 2 (no clear match)');
    expect(asked.some((url) => url.includes('side=architect'))).toBe(false);
    expect(architect!.textContent).not.toMatch(/Architect page 2/);
  });

  it('a vendor-only card never shows the vendor’s page under an architect page number', async () => {
    const { panel } = await openRow('v1', [PARTS]);
    const sources = [...panel.querySelectorAll('img')].map((img) => img.getAttribute('src') ?? '');
    expect(sources.some((src) => src.includes('/plan-pages/20'))).toBe(false);
    expect(asked.some((url) => url.includes('/plan-pages/20'))).toBe(false);
  });
});

describe('a spot off its pair', () => {
  it('is never shown as the architect’s picture; the pair’s page is, with a note', async () => {
    const off = finding('o1', {
      ...PAIRED, id: 'o1', arch_box: { page: 3, x0: 1, y0: 1, x1: 9, y1: 9, file: ARCH_B }, arch_picture_id: 'arch-o1',
      reference_page: { file: ARCH_B, page: 7, why: 'The architect’s spot is not on the page paired with this vendor page.', outside: true },
    });
    const { architect } = await openRow('o1', [off]);
    expect(architect!.querySelector('img')?.getAttribute('src')).toBe(`/api/v1/packages/p/plan-pages/7?file=${encodeURIComponent(ARCH_B)}`);
    expect(architect!.textContent).toContain('the item’s spot (page 3) is not on its paired page');
    expect(referenceWhere(off)).toEqual({ file: ARCH_B, page: 7 });
  });
});
