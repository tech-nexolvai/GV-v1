// @vitest-environment jsdom
/**
 * Chat v2/v3 on the screen (#1483): the findings table (groups, filters, counts, verified and source
 * labels, updates and drops by id), the evidence viewer (stepping, Esc, missing pictures, checks),
 * the coverage strip and its page grid, the trust summary and Trust column, the marked-up PDF and CSV
 * buttons, short answers and the phone layout. Synthetic data only.
 */
import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '@/api/client';
import { AssistantMessage } from '@/components/chat/chat-message';
import type { ChatCoverage, ChatFile, ChatFinding, ChatTrust } from '@/lib/chat-api';
import type { ViewMessage } from '@/lib/chat-messages';
import { finding, OPUS } from './fixtures/chat';

function answer(change: Partial<ViewMessage> = {}): ViewMessage {
  return {
    key: 'a', id: 'msg-1', role: 'assistant', created_at: '2026-10-17T10:00:00Z', text: '', model: 'claude-opus-5-5', effort: 'high', thinking: true,
    blocks: [{ type: 'text', text: 'Two items differ.' }], files: [], tasks: null, cost_usd: '1.72', stopped: null, error: null, status: 'done', ...change,
  };
}

function show(message: ViewMessage, props: Partial<React.ComponentProps<typeof AssistantMessage>> = {}) {
  const view = render(<AssistantMessage message={message} chatId="c-1" models={[OPUS]} messageCapUsd="5.00" {...props} />);
  return { node: screen.getByRole('article', { name: 'Answer' }), ...view };
}

const FINDINGS: ChatFinding[] = [
  finding('f1', { category: 'cabinet', page: 7, item: 'Base cabinet B3 width', architect: '30"', vendor: '33"' }),
  finding('f2', { category: 'countertop', page: 9, item: 'Island top depth', status: 'check', verified: false, created_by: 'helper:1' }),
  finding('f3', { category: 'countertop', page: 3, item: 'Vanity top depth', source: 'brand_prototype', architect: '22"', vendor: '21 1/2"', vendor_picture_id: null }),
  finding('f4', { category: 'other', page: 2, item: 'Sink model', status: 'same' }),
];

const rows = (node: HTMLElement) => [...node.querySelectorAll<HTMLElement>('[data-slot="chat-finding-row"]:not([data-leaving])')].map((row) => row.dataset.id);

let restoreMatchMedia: (() => void) | null = null;
afterEach(() => {
  restoreMatchMedia?.();
  restoreMatchMedia = null;
});

function mediaMatching(test: (query: string) => boolean) {
  const original = window.matchMedia;
  window.matchMedia = ((query: string) => ({
    matches: test(query), media: query, onchange: null,
    addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {}, dispatchEvent: () => false,
  })) as typeof window.matchMedia;
  restoreMatchMedia = () => { window.matchMedia = original; };
}

let clicks: string[] = [];
beforeEach(() => {
  clicks = [];
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) { clicks.push(`${this.download}|${this.getAttribute('href')}`); });
});

describe('the findings table', () => {
  it('is one table grouped Countertops / Cabinets / Other, with a count line, ticks, source tags and thumbnails', () => {
    const { node } = show(answer({ findings: FINDINGS }));
    const table = within(node).getByRole('table');
    expect(node.querySelectorAll('[data-slot="chat-findings-table"]')).toHaveLength(1);
    // Not inside the work fold: a direct part of the answer.
    expect(node.querySelector('[data-slot="chat-activity"] [data-slot="chat-findings"]')).toBeNull();
    expect(within(node).getByText('2 differences · 1 to check')).toBeTruthy();
    expect(within(table).getAllByRole('columnheader').map((cell) => cell.textContent)).toEqual(['Page', 'Item', 'Architect', 'Vendor', 'Status', 'Trust', 'Evidence']);
    expect(within(table).getAllByRole('rowheader').filter((cell) => cell.getAttribute('scope') === 'rowgroup').map((cell) => cell.textContent)).toEqual(['Countertops 2', 'Cabinets 1']);
    // Same rows are hidden until the filter asks.
    expect(rows(node)).toEqual(['f3', 'f2', 'f1']);

    const vanity = node.querySelector<HTMLElement>('[data-id="f3"]')!;
    expect(within(vanity).getByText('22"')).toBeTruthy();
    expect(within(vanity).getByText('21 1/2"')).toBeTruthy();
    expect(within(vanity).getByText('vs brand prototype')).toBeTruthy();
    expect(within(vanity).getByRole('img', { name: 'Architect evidence' }).getAttribute('src')).toBe('/api/v1/chats/c-1/pictures/arch-f3');
    expect(within(vanity).getByText('Vendor: no evidence picture')).toBeTruthy();

    const helper = node.querySelector<HTMLElement>('[data-id="f2"] [data-check="verified"]')!;
    expect(helper.getAttribute('title')).toBe('Helper lead, not checked');
    expect(node.querySelector('[data-id="f1"] [data-check="verified"]')?.getAttribute('title')).toBe('Checked on the drawing');
  });

  it('filters by status and category, and shows Same only when asked', async () => {
    const user = userEvent.setup();
    const { node } = show(answer({ findings: FINDINGS }));
    const chips = within(node).getByRole('group', { name: 'Filter findings' });
    await user.click(within(chips).getByRole('button', { name: /^Different/ }));
    expect(rows(node)).toEqual(['f3', 'f1']);
    await user.click(within(chips).getByRole('button', { name: /^Check/ }));
    expect(rows(node)).toEqual(['f2']);
    await user.click(within(chips).getByRole('button', { name: /^Same/ }));
    expect(rows(node)).toEqual(['f4']);
    await user.click(within(chips).getByRole('button', { name: /^All/ }));
    await user.click(within(chips).getByRole('button', { name: 'Cabinets' }));
    expect(rows(node)).toEqual(['f1']);
    expect(within(chips).getByRole('button', { name: 'Cabinets' }).getAttribute('aria-pressed')).toBe('true');
  });

  it('replaces an updated finding in place and fades a dropped one out', async () => {
    const { node, rerender } = show(answer({ findings: FINDINGS }));
    const updated = FINDINGS.map((item) => (item.id === 'f1' ? { ...item, vendor: '34"' } : item));
    rerender(<AssistantMessage message={answer({ findings: updated })} chatId="c-1" models={[OPUS]} />);
    expect(within(node.querySelector<HTMLElement>('[data-id="f1"]')!).getByText('34"')).toBeTruthy();

    rerender(<AssistantMessage message={answer({ findings: updated.filter((item) => item.id !== 'f2') })} chatId="c-1" models={[OPUS]} />);
    expect(node.querySelector('[data-id="f2"]')?.getAttribute('data-leaving')).toBe('true');
    expect(within(node).getByText('2 differences')).toBeTruthy();
    await waitFor(() => expect(node.querySelector('[data-id="f2"]')).toBeNull());
  });

  it('becomes stacked cards at phone width', () => {
    mediaMatching((query) => query.includes('max-width: 639px'));
    const { node } = show(answer({ findings: FINDINGS }));
    expect(within(node).queryByRole('table')).toBeNull();
    const cards = node.querySelector('[data-slot="chat-findings-cards"]')!;
    expect(within(cards as HTMLElement).getAllByRole('button', { name: /open the evidence/ })).toHaveLength(3);
    expect(within(cards as HTMLElement).getByText('Vanity top depth')).toBeTruthy();
  });
});

describe('the evidence viewer', () => {
  it('opens on a row, steps with the arrows, shows a placeholder for a missing picture, and closes on Esc', async () => {
    const user = userEvent.setup();
    const { node } = show(answer({ findings: FINDINGS }), { fullPageHref: '#/review/pkg-1' });
    await user.click(within(node).getByRole('button', { name: 'Vanity top depth' }));
    const viewer = await screen.findByRole('dialog');
    expect(within(viewer).getByRole('heading', { name: 'Vanity top depth' })).toBeTruthy();
    expect(within(viewer).getByRole('img', { name: 'Architect, page 3' }).getAttribute('src')).toBe('/api/v1/chats/c-1/pictures/arch-f3');
    expect(within(viewer).getByText('No evidence picture')).toBeTruthy();
    expect(within(viewer).getByText('1 of 3')).toBeTruthy();
    expect(within(viewer).getByRole('link', { name: 'Open in review screen' }).getAttribute('href')).toBe('#/review/pkg-1');

    await user.keyboard('{ArrowRight}');
    expect(within(screen.getByRole('dialog')).getByRole('heading', { name: 'Island top depth' })).toBeTruthy();
    await user.keyboard('{ArrowRight}');
    expect(within(screen.getByRole('dialog')).getByRole('heading', { name: 'Base cabinet B3 width' })).toBeTruthy();
    expect((within(screen.getByRole('dialog')).getByRole('button', { name: 'Next finding' }) as HTMLButtonElement).disabled).toBe(true);
    await user.keyboard('{ArrowLeft}');
    expect(within(screen.getByRole('dialog')).getByRole('heading', { name: 'Island top depth' })).toBeTruthy();

    // Zoom works on the large crop.
    const dialog = screen.getByRole('dialog');
    await user.click(within(dialog).getByRole('button', { name: 'Zoom in: Architect, page 9' }));
    expect(dialog.querySelector('[data-side="architect"] [data-slot="chat-evidence-frame"]')?.getAttribute('data-zoom')).toBe('1.50');

    await user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  });

  it('shows the check results under the crops', async () => {
    const user = userEvent.setup();
    const checked = finding('t', {
      item: 'Sink base width', architect: '3\'-1"', vendor: '46"', inches: { architect: '37', vendor: '46', difference: '9' },
      trust_level: 'confirmed', checks: { printed_architect: 'match', printed_vendor: 'match', second_read: 'agrees' },
    });
    const { node } = show(answer({ findings: [checked] }));
    await user.click(within(node).getByRole('button', { name: 'Sink base width' }));
    const viewer = await screen.findByRole('dialog');
    const printed = [...viewer.querySelectorAll('[data-slot="chat-evidence-printed"]')].map((line) => line.textContent);
    expect(printed).toEqual(['✓Printed at this spot: 3\'-1"', '✓Printed at this spot: 46"']);
    expect(within(viewer).getByText('Second read agrees')).toBeTruthy();
    expect(within(viewer).getByText('+9 in')).toBeTruthy();
  });
});

describe('the coverage strip', () => {
  const coverage: ChatCoverage = {
    pages: [
      { page: 1, status: 'same' }, { page: 2, status: 'same' }, { page: 3, status: 'different', note: 'Vanity depth' },
      { page: 7, status: 'different' }, { page: 8, status: 'not_comparable', note: 'Schedule only' }, { page: 9, status: 'not_opened' },
    ],
  };

  it('says how many pages were compared, opens to a page grid, and a different page filters the findings', async () => {
    const user = userEvent.setup();
    const { node } = show(answer({ findings: FINDINGS, coverage }));
    const strip = node.querySelector<HTMLElement>('[data-slot="chat-coverage"]')!;
    const toggle = within(strip).getByRole('button', { name: /pages compared/ });
    expect(toggle.textContent).toContain('4 of 6 pages compared · 1 not comparable · 1 not opened');
    expect(strip.querySelectorAll('[data-slot="chat-coverage-bar"] [data-status]')).toHaveLength(4);

    await user.click(toggle);
    expect(toggle.getAttribute('aria-expanded')).toBe('true');
    const squares = strip.querySelectorAll('[data-slot="chat-coverage-page"]');
    expect(squares).toHaveLength(6);
    expect(within(strip).getByRole('img', { name: 'Page 8: Not comparable. Schedule only' }).getAttribute('title')).toBe('Page 8: Not comparable. Schedule only');

    await user.click(within(strip).getByRole('button', { name: 'Page 7: Different. Show its findings' }));
    expect(rows(node)).toEqual(['f1']);
    await user.click(within(node).getByRole('button', { name: 'Showing page 7 only. Show all pages' }));
    expect(rows(node)).toEqual(['f3', 'f2', 'f1']);
  });
});

describe('trust', () => {
  const trusted: ChatFinding[] = [
    finding('c1', {
      item: 'Sink base width', architect: '3\'-1"', vendor: '46"', inches: { architect: '37', vendor: '46', difference: '9' }, round_status: 'still_wrong',
      trust_level: 'confirmed', checks: { printed_architect: 'match', printed_vendor: 'match', second_read: 'agrees' },
    }),
    finding('c2', {
      item: 'Filler', category: 'cabinet', status: 'check', verified: false, inches: { architect: '2', vendor: null, difference: null }, vendor: 'TBD', round_status: 'fixed_in_later_round',
      trust_level: 'to_check', checks: { second_read: 'disagrees' },
    }),
  ];
  const report: ChatTrust = {
    levels: { confirmed: 1, likely: 0, to_check: 1 },
    gv_checks: { matched: 8, total: 9, left_out: [{ page: 12, item: 'Upper cabinet height', values: 'architect 30" · vendor 36"', source: 'code rule' }] },
    not_checked: ['numbers drawn as pen strokes on page 4'],
  };

  it('sums it up above the table: levels, coverage, GV Review’s own checks and how it was checked', async () => {
    const user = userEvent.setup();
    const { node } = show(answer({ findings: trusted, trust: report, coverage: { pages: [{ page: 1, status: 'same' }, { page: 2, status: 'not_opened' }] } }));
    const summary = within(node).getByRole('region', { name: 'Why this can be trusted' });
    // Above the findings table.
    expect(summary.compareDocumentPosition(node.querySelector('[data-slot="chat-findings"]')!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect([...summary.querySelectorAll('[data-slot="chat-trust-levels"] li')].map((item) => item.textContent)).toEqual(['Confirmed1', 'Likely0', 'To check1']);
    expect(summary.querySelectorAll('[data-slot="chat-trust-bar"] [data-level]')).toHaveLength(2);
    expect(within(summary).getByText('1 of 2 pages compared · 0 not comparable · 1 not opened')).toBeTruthy();
    expect(within(summary).getByText('Vendor fixed 1 of 2 round-1 items · 1 still wrong')).toBeTruthy();

    const gv = within(summary).getByRole('button', { name: /Matches GV Review’s own checks: 8 of 9/ });
    await user.click(gv);
    expect(within(summary).getByText('Upper cabinet height')).toBeTruthy();

    await user.click(within(summary).getByRole('button', { name: 'How this was checked' }));
    const lines = [...summary.querySelectorAll('[data-slot="chat-trust-checks"] li')].map((line) => line.textContent);
    expect(lines).toContain('Printed at the spot: 2 of 2 values found');
    expect(lines).toContain('Second independent read: 1 agree · 1 disagree · 0 unsure');
    expect(lines).toContain('Not checked: numbers drawn as pen strokes on page 4');
    expect(lines).toContain('Not checked: page 2 (not opened)');
  });

  it('gives each row a Trust chip, check icons, code’s numbers and a round chip', () => {
    const { node } = show(answer({ findings: trusted, trust: report }));
    const table = within(node).getByRole('table');
    // Code's difference sits under the vendor's value: "37 in → 46 in · +9 in".
    expect(within(table).getAllByRole('columnheader')).toHaveLength(7);
    const sink = node.querySelector<HTMLElement>('[data-id="c1"]')!;
    expect(sink.querySelector('[data-slot="chat-trust-level"]')?.textContent).toBe('Confirmed');
    expect(within(sink).getByText('37 in').getAttribute('title')).toBe('As printed: 3\'-1"');
    expect(within(sink).getByText('+9 in')).toBeTruthy();
    expect(within(sink).getByText('Still wrong')).toBeTruthy();
    expect([...sink.querySelectorAll('[data-slot="chat-check"]')].map((icon) => `${icon.getAttribute('data-check')}:${icon.getAttribute('data-state')}`))
      .toEqual(['printed:pass', 'second_read:pass', 'verified:pass', 'rule:not_run']);

    const filler = node.querySelector<HTMLElement>('[data-id="c2"]')!;
    expect(filler.querySelector('[data-slot="chat-trust-level"]')?.getAttribute('title')).toBe('To check: Second read disagrees');
    expect(filler.querySelector('[data-check="second_read"]')?.getAttribute('title')).toBe('Second read disagrees');
    expect(within(filler).getByText('not a number')).toBeTruthy();
    expect(within(filler).getByText('TBD')).toBeTruthy();
  });

  it('downloads the findings as a CSV with the trust columns', async () => {
    const user = userEvent.setup();
    let saved: Blob | null = null;
    URL.createObjectURL = vi.fn((blob: Blob) => { saved = blob; return 'blob:csv'; }) as typeof URL.createObjectURL;
    URL.revokeObjectURL = vi.fn();
    const { node } = show(answer({ findings: trusted, trust: report }));
    await user.click(within(node).getByRole('button', { name: 'Download Excel (CSV)' }));
    expect(clicks).toEqual(['findings-msg-1.csv|blob:csv']);
    const text = await (saved as unknown as Blob).text();
    expect(text).toContain('Trust,Evidence,Printed at spot');
    expect(text).toContain('Sink base width');
  });
});

describe('the marked-up PDF button', () => {
  const made: ChatFile = { id: 'f-m', name: 'marked-up.pdf', size: 2048, mime: 'application/pdf', url: '/api/v1/chats/c-1/files/f-m' };

  it('asks the server, is disabled while it works, then shows the file card and downloads it', async () => {
    const user = userEvent.setup();
    let finish: (file: ChatFile) => void = () => {};
    const make = vi.fn(() => new Promise<ChatFile>((done) => { finish = done; }));
    const { node } = show(answer({ findings: FINDINGS }), { makeMarkupPdf: make });
    const button = within(node).getByRole('button', { name: 'Download marked-up PDF' });
    await user.click(button);
    expect(make).toHaveBeenCalledWith('msg-1');
    const busy = within(node).getByRole('button', { name: 'Making the marked-up PDF…' }) as HTMLButtonElement;
    expect(busy.disabled).toBe(true);
    await act(async () => finish(made));
    expect(within(node).getByRole('link', { name: 'Download marked-up.pdf' }).getAttribute('href')).toBe('/api/v1/chats/c-1/files/f-m');
    expect(clicks).toEqual(['marked-up.pdf|/api/v1/chats/c-1/files/f-m']);
    expect((within(node).getByRole('button', { name: 'Download marked-up PDF' }) as HTMLButtonElement).disabled).toBe(false);
  });

  it('says a failure in plain words', async () => {
    const user = userEvent.setup();
    const make = vi.fn(async () => { throw new ApiError(500, { error: 'internal', message: 'Traceback…', request_id: 'r' }); });
    const { node } = show(answer({ findings: FINDINGS }), { makeMarkupPdf: make });
    await user.click(within(node).getByRole('button', { name: 'Download marked-up PDF' }));
    expect((await within(node).findByRole('alert')).textContent).toBe('The marked-up PDF could not be made right now. Try again.');
  });

  it('is not offered while the answer runs, or without findings', () => {
    const make = vi.fn();
    const { node, rerender } = show(answer({ findings: FINDINGS, status: 'running' }), { makeMarkupPdf: make });
    expect(within(node).queryByRole('button', { name: 'Download marked-up PDF' })).toBeNull();
    rerender(<AssistantMessage message={answer()} chatId="c-1" models={[OPUS]} makeMarkupPdf={make} />);
    expect(within(node).queryByRole('button', { name: 'Download marked-up PDF' })).toBeNull();
    expect(within(node).queryByRole('button', { name: 'Download Excel (CSV)' })).toBeNull();
  });
});

describe('short answers', () => {
  it('are just the words and the meta line: no fold, tight spacing', () => {
    const { node } = show(answer({ blocks: [{ type: 'thinking', text: 'Easy.', seconds: 2, live: false }, { type: 'text', text: 'Yes: the island top is 36".' }] }));
    expect(node.getAttribute('data-short')).toBe('true');
    expect(within(node).queryByText('Thought for 2s')).toBeNull();
    expect(node.querySelector('[data-slot="chat-thinking"]')).toBeNull();
    expect(node.querySelector('[data-slot="chat-markdown"]')?.className).toContain('gap-2');
    expect(node.querySelector('[data-slot="chat-meta"]')?.textContent).toBe('Claude Opus 5.5 · High effort · $1.72');
    expect(node.querySelector('[data-slot="chat-findings"], [data-slot="chat-coverage"], [data-slot="chat-trust"]')).toBeNull();
  });

  it('a longer answer keeps its work row', () => {
    const { node } = show(answer({ blocks: [{ type: 'thinking', text: 'Hmm.', seconds: 2, live: false }, { type: 'text', text: 'One.\n\nTwo.\n\nThree.' }] }));
    expect(node.getAttribute('data-short')).toBeNull();
    expect(node.querySelector('[data-slot="chat-thinking"]')).toBeTruthy();
  });
});
