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

/** Opens a row's card, then the evidence viewer from it (a row click opens the card in place). */
async function openViewer(user: ReturnType<typeof userEvent.setup>, node: HTMLElement, item: string) {
  await user.click(within(node).getByRole('button', { name: item }));
  await user.click(within(node).getByRole('button', { name: 'Open viewer' }));
}

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

describe('the findings (#1522 redesign)', () => {
  it('shows What’s wrong, then Not compared, then Right folded, each grouped by area, one plain line per item', async () => {
    const user = userEvent.setup();
    const { node } = show(answer({ findings: FINDINGS }));
    expect(node.querySelectorAll('[data-slot="chat-findings"]')).toHaveLength(1);
    // Not inside the work fold: a direct part of the answer.
    expect(node.querySelector('[data-slot="chat-activity"] [data-slot="chat-findings"]')).toBeNull();
    expect(within(node).getByRole('heading', { name: /^Wrong\s*2$/ })).toBeTruthy();
    expect(within(node).getByRole('heading', { name: /^Not compared\s*1$/ })).toBeTruthy();
    // "Vanity top depth" names an area; "Base cabinet B3 width" names none, so it is grouped by page.
    const wrong = node.querySelector<HTMLElement>('[data-slot="chat-findings-different"]')!;
    expect([...wrong.querySelectorAll('[data-slot="chat-findings-group-toggle"]')].map((toggle) => toggle.textContent)).toEqual(['Vanityp.31', 'Page 71']);
    // Right is folded until asked.
    expect(rows(node)).toEqual(['f3', 'f1', 'f2']);
    await user.click(within(node).getByRole('button', { name: /^Right\s*1/ }));
    expect(rows(node)).toEqual(['f3', 'f1', 'f2', 'f4']);

    const vanity = node.querySelector<HTMLElement>('[data-id="f3"]')!;
    // The area is not repeated in the row; the full item stays the button's name.
    expect(within(vanity).getByRole('button', { name: 'Vanity top depth' }).textContent).toContain('Top depth');
    expect(vanity.querySelector('[data-slot="chat-finding-line"]')?.textContent).toBe('Vendor drew 21 1/2" · , brand prototype shows 22"');
    expect(within(vanity).getByText('Compared with the brand prototype')).toBeTruthy();
    expect(within(vanity).getByRole('img', { name: 'Architect evidence' }).getAttribute('src')).toBe('/api/v1/chats/c-1/pictures/arch-f3');
    expect(within(vanity).getByText('Vendor: no evidence picture')).toBeTruthy();
    // What the architect shows, on a Not-compared row.
    expect(node.querySelector('[data-id="f2"] [data-slot="chat-finding-line"]')?.textContent).toContain('architect shows');
  });

  it('uses one vocabulary: never "To check", one coverage display and one accuracy badge', async () => {
    const user = userEvent.setup();
    const coverage: ChatCoverage = { pages: [{ page: 2, status: 'same' }, { page: 3, status: 'different' }, { page: 7, status: 'different' }, { page: 9, status: 'not_comparable' }] };
    const trusted = FINDINGS.map((each, at) => ({ ...each, trust_level: (['confirmed', 'likely', 'to_check', 'to_check'] as const)[at] }));
    const { node } = show(answer({ findings: trusted, coverage, trust: { levels: { confirmed: 1, likely: 1, to_check: 2 } }, accuracy: { score: 82, claims: 4, breakdown: [] } }));
    await user.click(within(node).getByRole('button', { name: /^Right\s*1/ }));
    await user.click(within(node).getByRole('button', { name: 'How this was checked' }));
    const words = node.textContent ?? '';
    expect(words).not.toMatch(/To check|\bDifferent\b|\bSame\b|Trust\b/);
    expect(words).toContain('AI reading only');
    expect(node.querySelectorAll('[data-slot="chat-coverage"]')).toHaveLength(1);
    expect(node.querySelectorAll('[data-slot="chat-accuracy"]')).toHaveLength(1);
    // No check icons on a row: confidence is a quiet meter with its word.
    expect(node.querySelector('[data-slot="chat-checks"], [data-slot="chat-check-legend"], [data-slot="chat-trust-level"]')).toBeNull();
    expect([...node.querySelectorAll('[data-slot="chat-finding-row"] [data-slot="chat-confidence"]')].map((meter) => meter.textContent)).toEqual(['Confidence: AI reading only', 'Confidence: Confirmed', 'Confidence: Likely', 'Confidence: AI reading only']);
  });

  it('filters by product, keeping the sections', async () => {
    const user = userEvent.setup();
    const { node } = show(answer({ findings: FINDINGS }));
    const chips = within(node).getByRole('group', { name: 'Filter findings' });
    await user.click(within(chips).getByRole('button', { name: 'Cabinets' }));
    expect(rows(node)).toEqual(['f1']);
    expect(within(chips).getByRole('button', { name: 'Cabinets' }).getAttribute('aria-pressed')).toBe('true');
    await user.click(within(chips).getByRole('button', { name: 'All' }));
    expect(rows(node)).toEqual(['f3', 'f1', 'f2']);
  });

  it('replaces an updated finding in place and fades a dropped one out', async () => {
    const { node, rerender } = show(answer({ findings: FINDINGS }));
    const updated = FINDINGS.map((item) => (item.id === 'f1' ? { ...item, vendor: '34"' } : item));
    rerender(<AssistantMessage message={answer({ findings: updated })} chatId="c-1" models={[OPUS]} />);
    expect(node.querySelector('[data-id="f1"] [data-slot="chat-finding-line"]')?.textContent).toContain('34"');

    rerender(<AssistantMessage message={answer({ findings: updated.filter((item) => item.id !== 'f1') })} chatId="c-1" models={[OPUS]} />);
    expect(node.querySelector('[data-id="f1"]')?.getAttribute('data-leaving')).toBe('true');
    expect(within(node).getByRole('heading', { name: /^Wrong\s*1$/ })).toBeTruthy();
    await waitFor(() => expect(node.querySelector('[data-id="f1"]')).toBeNull());
  });

  it('keeps every row on a phone, without the product filter', () => {
    mediaMatching((query) => query.includes('max-width: 639px'));
    const { node } = show(answer({ findings: FINDINGS }));
    expect(within(node).queryByRole('group', { name: 'Filter findings' })).toBeNull();
    expect(rows(node)).toEqual(['f3', 'f1', 'f2']);
  });
});

describe('the item card', () => {
  const sink = finding('c1', {
    item: 'Sink base width', architect: '3\'-1"', vendor: '46"', inches: { architect: '37', vendor: '46', difference: '9' }, round_status: 'still_wrong',
    trust_level: 'confirmed', checks: { printed_architect: 'match', printed_vendor: 'match', second_read: 'agrees' },
    arch_box: { page: 3, x0: 0, y0: 0, x1: 52, y1: 52 },
  });
  const filler = finding('c2', {
    item: 'Filler', category: 'cabinet', status: 'check', verified: false, inches: { architect: '2', vendor: null, difference: null }, vendor: 'TBD',
    why: 'The architect gives no filler: confirm the 3" filler', trust_level: 'to_check', checks: { second_read: 'disagrees', printed_architect: 'not_a_number', printed_vendor: 'no_text' },
  });

  it('says a wrong item in the reviewer’s words: what each side shows, why, what to tell the vendor, how sure', async () => {
    const user = userEvent.setup();
    const { node } = show(answer({ findings: [sink, filler, finding('x', { page: 9 }), finding('y', { page: 12 })] }));
    const row = node.querySelector<HTMLElement>('[data-id="c1"]')!;
    expect(row.querySelector('[data-slot="chat-finding-line"]')?.textContent).toBe('Vendor drew 46 in · , needs 37 in (architect 3\'-1")+9 in');
    expect(within(row).getByText('Previous comment not addressed')).toBeTruthy();
    const toggle = within(row).getByRole('button', { name: 'Sink base width' });
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    await user.click(toggle);
    expect(toggle.getAttribute('aria-expanded')).toBe('true');
    const card = node.querySelector<HTMLElement>('[data-slot="chat-finding-detail"][data-id="c1"]')!;
    const terms = [...card.querySelectorAll('dt')].map((term) => term.textContent);
    expect(terms).toEqual(['Architect shows', 'Vendor drew', 'Why it’s wrong', 'What to tell the vendor']);
    expect(within(card).getByText('37 in (printed 3\'-1")')).toBeTruthy();
    expect(card.querySelector('[data-slot="chat-finding-tell"]')?.textContent).toBe('Change it to 37 in to match the architect (now 46 in).');
    expect(card.querySelector('[data-slot="chat-finding-sure"]')?.textContent).toContain('How sure: Confirmed.');
    expect(within(card).getByText('Code found both values printed at their spots on the drawings.')).toBeTruthy();
    expect(within(card).getByRole('img', { name: 'Architect drawing, page 3' }).getAttribute('src')).toBe('/api/v1/chats/c-1/pictures/arch-c1');
  });

  it('says why on a Not-compared item (never a question), never shows internal check names, and opens the viewer', async () => {
    const user = userEvent.setup();
    const { node } = show(answer({ findings: [sink, filler, finding('x', { page: 9 }), finding('y', { page: 12 })] }));
    const row = node.querySelector<HTMLElement>('[data-id="c2"]')!;
    expect(row.querySelector('[data-slot="chat-finding-question"]')?.textContent).toMatch(/^Why: /);
    expect(row.textContent).not.toMatch(/Your call|your decision/i);
    await user.click(within(row).getByRole('button', { name: 'Filler' }));
    const card = node.querySelector<HTMLElement>('[data-slot="chat-finding-detail"][data-id="c2"]')!;
    expect([...card.querySelectorAll('dt')].map((term) => term.textContent)).toEqual(['Architect shows', 'Vendor drew', 'Why it could not be compared']);
    expect(card.textContent).not.toMatch(/not a number|2nd read|Helper lead|Printed at this spot/);
    expect(within(card).getByText('A second, separate AI reading disagrees.')).toBeTruthy();
    expect(within(card).getByText('The vendor’s numbers are drawn as pen strokes, so code could not read them.')).toBeTruthy();
    await user.click(within(card).getByRole('button', { name: 'Open viewer' }));
    expect(within(await screen.findByRole('dialog')).getByRole('heading', { name: /^Filler\s*· page 3$/ })).toBeTruthy();
  });

  it('offers Agree / Not a problem on every finished answer (no probe request), and records the answer', async () => {
    const user = userEvent.setup();
    const decide = vi.fn(async () => ({}));
    const fetchSpy = vi.spyOn(globalThis, 'fetch');
    const { node } = show(answer({ findings: FINDINGS }), { decide });
    expect(node.querySelectorAll('[data-slot="chat-finding-decision"]')).toHaveLength(3);
    // Nothing is asked of the server just to show the buttons.
    expect(fetchSpy).not.toHaveBeenCalled();
    fetchSpy.mockRestore();
    const vanity = node.querySelector<HTMLElement>('[data-id="f3"]')!;
    await user.click(within(vanity).getByRole('button', { name: 'Agree' }));
    expect(decide).toHaveBeenCalledWith('msg-1', 'f3', 'agree');
    await waitFor(() => expect(within(vanity).getByRole('button', { name: 'Agree' }).getAttribute('aria-pressed')).toBe('true'));
    // Keyboard: Tab to "Not a problem", Space presses it.
    within(vanity).getByRole('button', { name: 'Agree' }).focus();
    await user.tab();
    await user.keyboard(' ');
    expect(decide).toHaveBeenLastCalledWith('msg-1', 'f3', 'not_a_problem');
  });

  it('hides the buttons again, and says so once, when the server has no decision route (404 / 405)', async () => {
    const user = userEvent.setup();
    const decide = vi.fn(async () => { throw new ApiError(405, { error: 'method_not_allowed', message: 'Method Not Allowed', request_id: 'r' }); });
    const { node } = show(answer({ findings: FINDINGS }), { decide });
    await user.click(within(node.querySelector<HTMLElement>('[data-id="f3"]')!).getByRole('button', { name: 'Agree' }));
    await waitFor(() => expect(within(node).getByRole('alert').textContent).toBe('Answers cannot be saved on this server yet.'));
    expect(node.querySelector('[data-slot="chat-finding-decision"]')).toBeNull();
  });

  it('is not offered while the answer runs', () => {
    const { node } = show(answer({ findings: FINDINGS, status: 'running' }), { decide: vi.fn() });
    expect(node.querySelector('[data-slot="chat-finding-decision"]')).toBeNull();
  });
});

describe('a narrow answer (a few pages)', () => {
  it('shows only its words, its findings with their cards open, and one confidence line: no whole-set panels', () => {
    const few = [
      finding('n1', { page: 8, item: 'Bussing station filler width', status: 'check', trust_level: 'to_check' }),
      finding('n2', { page: 8, item: 'Bussing station door count', status: 'same', trust_level: 'likely' }),
    ];
    const coverage: ChatCoverage = { pages: [{ page: 8, status: 'different' }, { page: 9, status: 'not_opened' }] };
    const { node } = show(answer({ findings: few, coverage, trust: { levels: { confirmed: 0, likely: 1, to_check: 1 }, gv_checks: { matched: 11, total: 13, left_out: [] } } }));
    expect(node.getAttribute('data-narrow')).toBe('true');
    expect(node.querySelector('[data-slot="chat-trust"], [data-slot="chat-coverage"], [data-slot="chat-coverage-grid"]')).toBeNull();
    expect(node.textContent).not.toMatch(/Matches GV Review|pages compared|Read and checked by code/);
    expect(node.querySelector('[data-slot="chat-confidence-line"]')?.textContent).toBe('How sure: 1 likely · 1 AI reading only');
    expect(node.querySelector('[data-slot="chat-finding-detail"][data-id="n1"]')).toBeTruthy();
  });
});

describe('the evidence viewer', () => {
  it('opens on a row, steps with the arrows, shows a placeholder for a missing picture, and closes on Esc', async () => {
    const user = userEvent.setup();
    const { node } = show(answer({ findings: FINDINGS }), { fullPageHref: '#/review/pkg-1' });
    await openViewer(user, node, 'Vanity top depth');
    const viewer = await screen.findByRole('dialog');
    expect(within(viewer).getByRole('heading', { name: /^Vanity top depth\s*· page 3$/ })).toBeTruthy();
    expect(within(viewer).getByRole('img', { name: 'Architect, page 3' }).getAttribute('src')).toBe('/api/v1/chats/c-1/pictures/arch-f3');
    expect(within(viewer).getByText('No evidence picture')).toBeTruthy();
    expect(within(viewer).getByText('1 of 4')).toBeTruthy();
    expect(within(viewer).getByRole('link', { name: 'Open in review screen' }).getAttribute('href')).toBe('#/review/pkg-1');

    // The arrows follow the screen's order: Wrong by area, then Not compared, then Right.
    await user.keyboard('{ArrowRight}');
    expect(within(screen.getByRole('dialog')).getByRole('heading', { name: /^Base cabinet B3 width\s*· page 7$/ })).toBeTruthy();
    await user.keyboard('{ArrowRight}');
    expect(within(screen.getByRole('dialog')).getByRole('heading', { name: /^Island top depth\s*· page 9$/ })).toBeTruthy();
    await user.keyboard('{ArrowRight}');
    expect((within(screen.getByRole('dialog')).getByRole('button', { name: 'Next finding' }) as HTMLButtonElement).disabled).toBe(true);
    await user.keyboard('{ArrowLeft}');
    await user.keyboard('{ArrowLeft}');
    expect(within(screen.getByRole('dialog')).getByRole('heading', { name: /^Base cabinet B3 width\s*· page 7$/ })).toBeTruthy();

    // Zoom works on the large crop.
    const dialog = screen.getByRole('dialog');
    await user.click(within(dialog).getByRole('button', { name: 'Zoom in: Architect, page 7' }));
    expect(dialog.querySelector('[data-side="architect"] [data-slot="chat-evidence-frame"]')?.getAttribute('data-zoom')).toBe('1.50');

    await user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  });

  it('says the item in the reviewer’s words, with how we know folded', async () => {
    const user = userEvent.setup();
    const checked = finding('t', {
      item: 'Sink base width', architect: '3\'-1"', vendor: '46"', inches: { architect: '37', vendor: '46', difference: '9' },
      trust_level: 'confirmed', checks: { printed_architect: 'match', printed_vendor: 'match', second_read: 'agrees' },
    });
    const { node } = show(answer({ findings: [checked] }));
    // A one-item answer is narrow: its card is already open.
    await user.click(within(node).getByRole('button', { name: 'Open viewer' }));
    const viewer = await screen.findByRole('dialog');
    expect(within(viewer).getByText('37 in (printed 3\'-1")')).toBeTruthy();
    expect(within(viewer).getByText('Change it to 37 in to match the architect (now 46 in).')).toBeTruthy();
    expect(within(viewer).getByText('A second, separate AI reading agrees.')).toBeTruthy();
    expect(viewer.textContent).not.toMatch(/Printed at this spot|not a number/);
  });
});

describe('how this was checked (whole-set answers)', () => {
  const coverage: ChatCoverage = {
    pages: [
      { page: 1, status: 'same' }, { page: 2, status: 'same' }, { page: 3, status: 'different', note: 'Vanity depth' },
      { page: 7, status: 'different' }, { page: 8, status: 'not_comparable', note: 'Schedule only' }, { page: 9, status: 'not_opened' },
    ],
  };
  const trusted: ChatFinding[] = [
    finding('c1', {
      item: 'Sink base width', page: 2, architect: '3\'-1"', vendor: '46"', inches: { architect: '37', vendor: '46', difference: '9' }, round_status: 'still_wrong',
      trust_level: 'confirmed', checks: { printed_architect: 'match', printed_vendor: 'match', second_read: 'agrees' },
    }),
    finding('c2', {
      item: 'Filler', page: 5, category: 'cabinet', status: 'check', verified: false, inches: { architect: '2', vendor: null, difference: null }, vendor: 'TBD', round_status: 'fixed_in_later_round',
      trust_level: 'to_check', checks: { second_read: 'disagrees' },
    }),
    finding('c3', { page: 7, item: 'Island depth' }),
    finding('c4', { page: 11, item: 'Desk width' }),
  ];
  const report: ChatTrust = {
    levels: { confirmed: 1, likely: 0, to_check: 1 },
    gv_checks: { matched: 8, total: 9, left_out: [{ page: 12, item: 'Upper cabinet height', values: 'architect 30" · vendor 36"', source: 'code rule' }] },
    not_checked: ['numbers drawn as pen strokes on page 4'],
  };

  it('is one folded section after the findings: how sure, pages (the only coverage), GV Review’s own checks and the checks', async () => {
    const user = userEvent.setup();
    const { node } = show(answer({ findings: trusted, trust: report, coverage }));
    const how = within(node).getByRole('region', { name: 'How this was checked' });
    expect(node.querySelector('[data-slot="chat-findings"]')!.compareDocumentPosition(how) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(how.querySelector('[data-slot="chat-trust-levels"]')).toBeNull();
    await user.click(within(how).getByRole('button', { name: 'How this was checked' }));
    expect([...how.querySelectorAll('[data-slot="chat-trust-levels"] li')].map((item) => item.textContent)).toEqual(['Confirmed1', 'Likely0', 'AI reading only1']);
    expect(within(how).getByText('Closed 1 of 2 round-1 items · 1 previous comment not addressed')).toBeTruthy();
    await user.click(within(how).getByRole('button', { name: /Matches GV Review’s own checks: 8 of 9/ }));
    expect(within(how).getByText('Upper cabinet height')).toBeTruthy();
    const lines = [...how.querySelectorAll('[data-slot="chat-trust-checks"] li')].map((line) => line.textContent);
    expect(lines).toContain('Printed at the spot: 2 of 2 values found');
    expect(lines).toContain('Not checked: numbers drawn as pen strokes on page 4');
    expect(lines).toContain('Not checked: page 9 (not opened)');

    // The page grid: a different page narrows the findings.
    const strip = how.querySelector<HTMLElement>('[data-slot="chat-coverage"]')!;
    const toggle = within(strip).getByRole('button', { name: /pages compared/ });
    expect(toggle.textContent).toContain('4 of 6 pages compared · 1 not comparable · 1 not opened');
    await user.click(toggle);
    expect(strip.querySelectorAll('[data-slot="chat-coverage-page"]')).toHaveLength(6);
    await user.click(within(strip).getByRole('button', { name: 'Page 7: Different. Show its findings' }));
    expect(rows(node)).toEqual(['c3']);
    await user.click(within(node).getByRole('button', { name: 'Showing page 7 only. Show all pages' }));
    expect(rows(node)).toEqual(['c1', 'c3', 'c4', 'c2']);
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
