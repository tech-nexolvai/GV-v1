// @vitest-environment jsdom
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest';

import type { AiReviewOut, AiReviewPage, AiReviewRow, ArchitectCompareEntry, ReviewItemsOut } from '@/api/review-items';
import { AiReview } from '@/components/simple-review/ai-review';
import { aiReviewSummary } from '@/components/simple-review/ai-review-lib';
import { reviewItems } from './fixtures/review-items';

// Synthetic data only: every number, page, name and span here is invented.
vi.mock('@/api/config', () => ({ projectId: () => 'proj-demo' }));

const { SimpleReviewPage } = await import('@/pages/SimpleReviewPage');

const ref = (dv: string, page: number) => ({ document_version_id: dv, page_number: page, polygon: [['0.1', '0.1'], ['0.2', '0.1'], ['0.2', '0.2'], ['0.1', '0.2']] });

const WHY: Record<AiReviewRow['result'], string> = {
  same: 'Both say the same.',
  different: 'The vendor drew a different number for this span.',
  not_shown: 'The vendor shows no number for this span.',
  unsure: 'The AI could not tell which vendor number is this span.',
  unreadable: 'The vendor number is hidden under markup.',
};

function row(id: string, result: AiReviewRow['result'], extra: Partial<AiReviewRow> = {}): AiReviewRow {
  const shown = result === 'same' || result === 'different';
  return {
    key: `air:${id}`,
    architect_text: '30"',
    architect_inches: '30/1',
    measures: 'sample overall width',
    vendor_text: shown ? (result === 'different' ? '30 1/2"' : '30"') : null,
    vendor_inches: shown ? (result === 'different' ? '61/2' : '30/1') : null,
    result,
    why: WHY[result],
    vendor_box: shown ? ref('docv-vendor', 3) : null,
    architect_ref: ref('docv-vendor', 3),
    actions: result === 'different' ? ['agree', 'not_this'] : [],
    status: result === 'different' ? 'open' : 'done',
    answer: null,
    answered_by: null,
    ...extra,
  };
}

function page(n: number, status: AiReviewPage['status'], rows: AiReviewRow[] = [], extra: Partial<AiReviewPage> = {}): AiReviewPage {
  return { page: n, status, cost_usd: status === 'done' ? '0.40' : null, rows, ...extra };
}

const PAGES: AiReviewPage[] = [
  page(3, 'done', [row('s1', 'same'), row('d1', 'different'), row('n1', 'not_shown'), row('u1', 'unsure'), row('r1', 'unreadable'), row('s2', 'same')]),
  page(4, 'done', [row('d2', 'different', { status: 'done', actions: [], answer: 'not_this', answered_by: 'Sample Reviewer' })]),
  page(5, 'running'),
  page(6, 'failed', [], { reason: 'the page picture could not be made' }),
];
// Deliberately not the rows' own counts: the line prints what the server sent.
const REVIEW: AiReviewOut = { pages: PAGES, counts: { same: 40, different: 2, not_shown: 30, unsure: 4, unreadable: 1, needs_person: 1 } };

describe('AiReview', () => {
  it('prints the one-line summary from the server counts', () => {
    render(<AiReview review={REVIEW} onLook={() => {}} />);
    expect(screen.getByRole('heading', { level: 2, name: 'Compared with the architect' })).toBeTruthy();
    expect(screen.getByTestId('ai-review-summary').textContent).toBe('AI read 2 pages: 2 different · 40 same · 30 not shown by the vendor · 5 unsure');
    expect(within(screen.getByTestId('ai-review-summary')).getByText('40').className).toContain('num');
  });

  it('says one page in the singular', () => {
    expect(aiReviewSummary({ pages: [page(1, 'done')], counts: { same: 0, different: 1, not_shown: 0, unsure: 0, unreadable: 0, needs_person: 1 } })).toBe(
      'AI read 1 page: 1 different · 0 same · 0 not shown by the vendor · 0 unsure',
    );
  });

  it('opens Different, folds Unsure, Same and Not shown', async () => {
    render(<AiReview review={REVIEW} onLook={() => {}} />);
    expect(screen.getByRole('button', { name: /^Different/ }).getAttribute('aria-expanded')).toBe('true');
    const rows = within(screen.getByRole('list', { name: 'Different' })).getAllByRole('listitem');
    expect(rows).toHaveLength(2);
    // Glyph and word, never colour alone; the numbers in the number face; what it measures; the page.
    expect(within(rows[0]).getByText('Different').querySelector('svg')).toBeTruthy();
    expect(within(rows[0]).getByText('30 1/2"').className).toContain('num');
    expect(rows[0].textContent).toContain('Architect 30" vs vendor 30 1/2"');
    expect(rows[0].textContent).toContain('sample overall width');
    expect(rows[0].textContent).toContain('p.3');

    const unsureButton = screen.getByRole('button', { name: /^Unsure \/ unreadable/ });
    expect(unsureButton.getAttribute('aria-expanded')).toBe('false');
    expect(screen.queryByRole('list', { name: 'Unsure / unreadable' })).toBeNull();
    await userEvent.click(unsureButton);
    const unsure = screen.getByRole('list', { name: 'Unsure / unreadable' });
    expect(unsure.textContent).toContain(WHY.unsure);
    expect(unsure.textContent).toContain(WHY.unreadable);
    expect(within(unsure).getByText('Unsure').querySelector('svg')).toBeTruthy();
    expect(within(unsure).getByText('Unreadable').querySelector('svg')).toBeTruthy();
    // Shown, not blocking: no answer buttons.
    expect(within(unsure).queryByRole('button', { name: /Agree|Not a difference/ })).toBeNull();

    const same = screen.getByRole('button', { name: /^Same/ });
    expect(same.getAttribute('aria-expanded')).toBe('false');
    expect(screen.queryByRole('list', { name: 'Same' })).toBeNull();
    await userEvent.click(same);
    expect(within(screen.getByRole('list', { name: 'Same' })).getAllByRole('listitem')).toHaveLength(2);

    const notShown = screen.getByRole('button', { name: /^Not shown by the vendor/ });
    expect(notShown.getAttribute('aria-expanded')).toBe('false');
    await userEvent.click(notShown);
    expect(screen.getByRole('list', { name: 'Not shown by the vendor' }).textContent).toContain(WHY.not_shown);
  });

  it('says which pages the AI is still reading and which it could not read, with no retry', () => {
    render(<AiReview review={REVIEW} onLook={() => {}} />);
    const status = screen.getByTestId('ai-review-pages');
    expect(status.textContent).toContain('AI is reading page 5…');
    expect(status.textContent).toContain("Couldn't read page 6: the page picture could not be made");
    expect(within(status).queryByRole('button')).toBeNull();
  });

  it('different rows need no person: only Look, never Agree or Not a difference, even if the server lists them', () => {
    const onAct = vi.fn();
    const review: AiReviewOut = { ...REVIEW, pages: [...REVIEW.pages, page(7, 'done', [row('d3', 'different', { actions: ['agree', 'not_this'] })])] };
    render(<AiReview review={review} onLook={() => {}} />);
    const rows = within(screen.getByRole('list', { name: 'Different' })).getAllByRole('listitem');
    expect(rows).toHaveLength(3);
    for (const listed of rows) {
      expect(within(listed).queryByRole('button', { name: /Agree|Not a difference/ })).toBeNull();
      expect(within(listed).getAllByRole('button').map((b) => b.textContent)).toEqual(['Look']);
      expect(within(listed).queryByTestId('ai-review-answered')).toBeNull();
    }
    expect(screen.queryByRole('button', { name: /Agree|Not a difference/ })).toBeNull();
    expect(onAct).not.toHaveBeenCalled();
  });

  it('Look hands back the row it was pressed on, from the keyboard too', async () => {
    const onLook = vi.fn();
    render(<AiReview review={REVIEW} onLook={onLook} />);
    const look = screen.getByRole('button', { name: 'Look at architect 30" vs vendor 30 1/2", page 3' });
    look.focus();
    await userEvent.keyboard('{Enter}');
    expect(onLook).toHaveBeenCalledWith(expect.objectContaining({ key: 'air:d1' }), 3);
  });

  it('is not shown when absent or with no pages', () => {
    const { container, rerender } = render(<AiReview review={null} onLook={() => {}} />);
    expect(container.innerHTML).toBe('');
    rerender(<AiReview review={{ pages: [], counts: REVIEW.counts }} onLook={() => {}} />);
    expect(container.innerHTML).toBe('');
  });
});

describe('SimpleReviewPage: the AI page review', () => {
  const API = '*/api/v1/projects/proj-demo/packages/pkg-demo';
  let current: ReviewItemsOut;
  let posted: { key: string; action: string; body: unknown }[] = [];
  const server = setupServer(
    http.get(`${API}/review-items`, () => HttpResponse.json(current)),
    http.post(`${API}/review-items/:key/:action`, async ({ params, request }) => {
      posted.push({ key: decodeURIComponent(String(params.key)), action: String(params.action), body: await request.json() });
      return HttpResponse.json({ ok: true });
    }),
    http.get(API, () => HttpResponse.json({ id: 'pkg-demo', vendor: 'Sample Stoneworks', current_revision_number: 2 })),
  );
  beforeAll(() => server.listen({ onUnhandledRequest: 'error' }));
  afterEach(() => server.resetHandlers());
  afterAll(() => server.close());

  const OLD: Partial<ArchitectCompareEntry> = {
    key: 'arch:old1', page: 3, view: null, architect_text: '30"', architect_inches: '30/1', vendor_text: null, vendor_inches: null,
    result: 'unpaired', rule_id: null, rule_outcome: null, why: 'Old direct compare row.', finding_id: null, architect_ref: null, vendor_ref: null, status: 'open', actions: [],
  };

  function withReview(review: AiReviewOut | null = REVIEW): ReviewItemsOut {
    return reviewItems((draft) => {
      Object.assign(draft, { architect_compare: [OLD], ai_review: review });
    });
  }

  function renderPage() {
    const onOpenPdfs = vi.fn();
    render(<SimpleReviewPage packageId="pkg-demo" pictures={async () => 'data:image/png;base64,iVBORw0KGgo='} assistantAvailable={false} onOpenAdvanced={vi.fn()} onOpenPdfs={onOpenPdfs} />);
    return onOpenPdfs;
  }

  it('is the first section and replaces the old direct compare', async () => {
    current = withReview();
    renderPage();
    const sections = await screen.findAllByRole('region', { name: 'Compared with the architect' });
    expect(sections).toHaveLength(1);
    expect(sections[0].getAttribute('data-slot')).toBe('ai-review');
    expect(screen.queryByText('Old direct compare row.')).toBeNull();
    const failures = screen.getByRole('region', { name: 'Will be clouded as wrong' });
    expect(sections[0].compareDocumentPosition(failures) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('keeps the old section for a package with no AI review', async () => {
    current = withReview(null);
    renderPage();
    const section = await screen.findByRole('region', { name: 'Compared with the architect' });
    expect(section.getAttribute('data-slot')).toBe('architect-compare');
  });

  it('Look opens both drawings at the row, the vendor box ringed', async () => {
    current = withReview();
    const onOpenPdfs = renderPage();
    const section = await screen.findByRole('region', { name: 'Compared with the architect' });
    await userEvent.click(within(section).getByRole('button', { name: 'Look at architect 30" vs vendor 30 1/2", page 3' }));
    expect(onOpenPdfs).toHaveBeenCalledWith(
      expect.objectContaining({
        key: 'air:d1',
        page: 3,
        vendor_ref: expect.objectContaining({ document_version_id: 'docv-vendor', page_number: 3 }),
        architect_ref: expect.objectContaining({ location: expect.objectContaining({ document_version_id: 'docv-vendor', page_number: 3 }) }),
      }),
    );
  });

  it('offers no answer on the page: nothing is sent for an AI review row', async () => {
    posted = [];
    current = withReview();
    renderPage();
    const section = await screen.findByRole('region', { name: 'Compared with the architect' });
    expect(within(section).queryByRole('button', { name: /Agree|Not a difference/ })).toBeNull();
    expect(posted).toHaveLength(0);
  });
});

describe('AiReview row layout (QA 2026-10-16)', () => {
  // jsdom has no layout, so the rules that keep a row readable are pinned on its classes: the row
  // is sized by its own section (a container), never by the window, so the Assistant panel beside
  // it cannot squeeze the numbers; the numbers keep a readable width and drop to their own line
  // instead of wrapping one word per line; nothing is clipped.
  it('sizes the row by the section, not the window, and lets the numbers take their own line', async () => {
    render(<AiReview review={REVIEW} onLook={() => {}} />);
    const section = screen.getByRole('region', { name: 'Compared with the architect' });
    expect(section.className).toContain('@container/air');
    const [first] = screen.getAllByRole('listitem').filter((li) => li.getAttribute('data-slot') === 'ai-review-row');
    expect(first.className).toContain('flex-wrap');
    expect(first.className).not.toMatch(/overflow-hidden|truncate/);
    const [word, numbers, look] = Array.from(first.children) as HTMLElement[];
    expect(word.className).toContain('w-full');
    expect(word.className).not.toMatch(/(^|\s)sm:/);
    expect(numbers.getAttribute('data-slot')).toBe('ai-review-numbers');
    expect(numbers.className).toContain('basis-[18rem]');
    expect(numbers.className).toContain('grow');
    expect(numbers.className).toContain('min-w-0');
    expect(look.className).toContain('ml-auto');
  });
});
